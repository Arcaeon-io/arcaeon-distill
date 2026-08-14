"""Tests for arcaeon-distill.

The product claim is determinism-under-budget plus an honest drop receipt, so
those are the load-bearing checks: same input -> byte-identical output across
repeated runs, and a receipt whose internal claims are self-consistent (or
caught when they aren't). Per-strategy coverage (json/tabular/text) proves
each path actually shrinks and actually reports what it cut.

Run: python test_distill.py
"""
import json

from arcaeon_distill import (
    distill, estimate_tokens, DropReceipt, verify_receipt, SCHEMA,
)


def _big_json():
    return {
        "status": "ok",
        "query": "weather stations",
        "summary": "A" * 2000,
        "results": [
            {"id": i, "name": f"station-{i}", "note": "reading " * 20}
            for i in range(200)
        ],
    }


def _big_rows():
    return [{"id": i, "value": i * 3, "label": f"row-{i}"} for i in range(300)]


def _big_text():
    lead = "The outage began at 03:14 UTC and affected the west region. "
    middle = " ".join(f"Diagnostic step {i} found nothing conclusive and "
                      f"logs for shard {i} were inconclusive as well."
                      for i in range(60))
    tail = " Root cause was a stale DNS record fixed at 04:02 UTC, and the incident closed clean."
    return lead + middle + tail


def test_determinism_json():
    data = _big_json()
    r1 = distill(data, budget=300)
    r2 = distill(_big_json(), budget=300)  # fresh equal-but-not-identical object
    assert json.dumps(r1.content, sort_keys=True) == json.dumps(r2.content, sort_keys=True)
    assert r1.receipt.to_dict() == r2.receipt.to_dict()
    print("PASS determinism: json strategy, same input -> identical output twice")


def test_determinism_tabular():
    r1 = distill(_big_rows(), budget=400)
    r2 = distill(_big_rows(), budget=400)
    assert json.dumps(r1.content, sort_keys=True) == json.dumps(r2.content, sort_keys=True)
    assert r1.receipt.to_dict() == r2.receipt.to_dict()
    print("PASS determinism: tabular strategy, same input -> identical output twice")


def test_determinism_text():
    text = _big_text()
    r1 = distill(text, budget=100, query="root cause")
    r2 = distill(text, budget=100, query="root cause")
    assert r1.content == r2.content
    assert r1.receipt.to_dict() == r2.receipt.to_dict()
    print("PASS determinism: text strategy, same input+query -> identical output twice")


def test_determinism_repeated_many_runs():
    data = _big_json()
    outs = {json.dumps(distill(data, budget=250).content, sort_keys=True) for _ in range(8)}
    assert len(outs) == 1, "8 runs of the same input produced different outputs"
    print("PASS determinism: 8 repeated runs collapse to one output")


def test_budget_adherence_json():
    result = distill(_big_json(), budget=200)
    assert result.est_tokens_after <= result.est_tokens_before
    assert result.truncated
    # best-effort, not a hard cap — assert it's in the right ballpark, not exact
    assert result.est_tokens_after < result.est_tokens_before * 0.5
    print(f"PASS budget adherence (json): {result.est_tokens_before} -> "
          f"{result.est_tokens_after} tokens, budget 200")


def test_budget_adherence_tabular():
    result = distill(_big_rows(), budget=300)
    assert result.truncated
    assert result.est_tokens_after < result.est_tokens_before
    print(f"PASS budget adherence (tabular): {result.est_tokens_before} -> "
          f"{result.est_tokens_after} tokens, budget 300")


def test_budget_adherence_text():
    result = distill(_big_text(), budget=80)
    assert result.truncated
    assert result.est_tokens_after < result.est_tokens_before
    print(f"PASS budget adherence (text): {result.est_tokens_before} -> "
          f"{result.est_tokens_after} tokens, budget 80")


def test_passthrough_under_budget():
    small = {"ok": True, "n": 3}
    result = distill(small, budget=2000)
    assert result.content == small
    assert not result.truncated
    assert result.receipt.drops == []
    v = verify_receipt(result.receipt)
    assert v["ok"], v
    print("PASS passthrough: small input under budget is returned unmodified, no drops")


def test_json_strategy_shape():
    result = distill(_big_json(), budget=150, schema_hint="json")
    assert result.strategy == "json"
    c = result.content
    assert set(c.keys()) == {"status", "query", "summary", "results"}
    assert c["summary"].endswith("more chars")
    assert any(isinstance(x, str) and "more items" in x for x in c["results"])
    kinds = {d["kind"] for d in result.receipt.drops}
    assert "string_truncated" in kinds
    assert "list_truncated" in kinds
    print("PASS json strategy: keys kept, long string capped with a count, "
          "long list head/tail with a dropped-item marker")


def test_tabular_strategy_shape_list_of_dicts():
    result = distill(_big_rows(), budget=400, schema_hint="tabular")
    assert result.strategy == "tabular"
    assert isinstance(result.content, list)
    assert result.content[0] == {"id": 0, "value": 0, "label": "row-0"}
    assert result.content[-1] == {"id": 299, "value": 897, "label": "row-299"}
    assert any("__distilled_dropped_rows__" in row for row in result.content
               if isinstance(row, dict))
    assert result.receipt.drops[0]["kind"] == "rows_dropped"
    print("PASS tabular strategy (list-of-dicts): head+tail rows kept, "
          "dropped-row marker present")


def test_tabular_strategy_shape_csv_text():
    header = "id,value,label"
    rows = [f"{i},{i*3},row-{i}" for i in range(200)]
    csv_text = "\n".join([header] + rows)
    result = distill(csv_text, budget=300)
    assert result.strategy == "tabular"
    assert result.content.startswith(header)
    assert "rows dropped" in result.content
    assert result.receipt.drops[0]["kind"] == "rows_dropped"
    print("PASS tabular strategy (CSV text): header kept, dropped-row count in body")


def test_text_strategy_reassembles_in_original_order():
    text = _big_text()
    result = distill(text, budget=100, schema_hint="text", query="DNS root cause")
    assert result.strategy == "text"
    assert result.content.startswith("The outage began")
    assert "stale DNS record" in result.content or "Root cause" in result.content
    assert result.receipt.drops[0]["kind"] == "sentences_dropped"
    print("PASS text strategy: extractive selection keeps lead + query-relevant "
          "sentence, reassembled in original order")


def test_drop_receipt_catches_planted_inconsistency():
    # Plant the lie: claim truncated=False while a drop is recorded (or the
    # reverse). verify_receipt must catch the internal contradiction.
    honest = distill(_big_json(), budget=150).receipt
    v_honest = verify_receipt(honest)
    assert v_honest["ok"], v_honest

    tampered = DropReceipt(**{**honest.to_dict(), "truncated": False})
    v_tampered = verify_receipt(tampered)
    assert not v_tampered["ok"]
    assert any("disagrees" in n for n in v_tampered["notes"]), v_tampered["notes"]
    print("PASS drop receipt: planted truncated/drops mismatch is caught by verify_receipt")


def test_drop_receipt_catches_corrupted_digest():
    receipt = distill(_big_rows(), budget=300).receipt
    row = receipt.to_dict()
    row["distilled"]["digest"] = "not-a-real-digest"
    v = verify_receipt(row)
    assert not v["ok"]
    assert any("well-formed" in n for n in v["notes"]), v["notes"]
    print("PASS drop receipt: malformed digest field is caught by verify_receipt")


def test_receipt_never_contains_full_content():
    result = distill(_big_json(), budget=150)
    blob = json.dumps(result.receipt.to_dict())
    assert "A" * 50 not in blob  # the long summary value never appears verbatim
    print("PASS drop receipt: dropped content is digested, never carried verbatim")


def test_receipt_optional():
    result = distill(_big_json(), budget=150, receipt=False)
    assert result.receipt is None
    print("PASS receipt=False skips receipt computation")


def test_estimate_tokens_heuristic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens({"a": 1}) > 0
    assert estimate_tokens(b"abcdefgh") == 2
    print("PASS estimate_tokens: cheap heuristic behaves sanely on str/dict/bytes")


def test_schema_hint_forces_strategy():
    text_like_json = json.dumps({"a": 1})
    result = distill(text_like_json, budget=2000, schema_hint="text")
    assert result.strategy == "text"
    print("PASS schema_hint overrides auto-detection")


def test_invalid_budget_rejected():
    try:
        distill({"a": 1}, budget=0)
    except ValueError:
        print("PASS budget<=0 raises ValueError")
        return
    raise AssertionError("expected ValueError for budget=0")


def test_seal_without_ledger_raises_clear_error():
    result = distill({"a": "b" * 500}, budget=10)
    try:
        result.receipt.seal("nonexistent.jsonl")
    except ImportError as e:
        assert "arcaeon-ledger" in str(e)
        print("PASS DropReceipt.seal() without arcaeon-ledger installed raises a clear ImportError")
        return
    print("PASS DropReceipt.seal() succeeded (arcaeon-ledger is installed in this env)")


ALL_TESTS = [v for k, v in list(globals().items()) if k.startswith("test_")]


def run() -> int:
    failures = 0
    for t in ALL_TESTS:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(ALL_TESTS) - failures}/{len(ALL_TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
