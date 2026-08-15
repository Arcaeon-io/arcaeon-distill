"""Tests for arcaeon-distill.

The product claim is determinism-under-budget plus an honest drop receipt, so
those are the load-bearing checks: same input -> byte-identical output across
repeated runs, and a receipt whose internal claims are self-consistent (or
caught when they aren't). Per-strategy coverage (json/tabular/text) proves
each path actually shrinks and actually reports what it cut.

Two extra layers of the determinism claim live here too (board item 19):

  - Cross-PROCESS determinism. Same-process determinism (below) only proves
    the algorithm doesn't depend on anything that varies call-to-call within
    one interpreter. It does NOT rule out dependence on something that
    varies process-to-process — PYTHONHASHSEED-driven set/dict iteration
    order being the classic one. `distill()` doesn't rely on hash-order
    directly (JSON dumps use sort_keys=True and structures are lists/dicts
    walked in insertion order), but the only way to actually PROVE that,
    rather than assert it, is to run it in two separate interpreters and
    diff the bytes.
  - Cross-VERSION stability (golden fixtures). The digest-based golden
    vectors in selftest.py prove byte-identity indirectly (through a hash);
    they don't show you what changed if a future edit alters the output.
    The GOLDEN_* constants below freeze the actual output values instead —
    the promise-keeper the README's cache-stability section points to.

Run: python test_distill.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from arcaeon_distill import (
    distill, estimate_tokens, DropReceipt, verify_receipt, SCHEMA,
)


def _stable_receipt(receipt) -> dict:
    """Receipt fields relevant to a determinism check, excluding the
    wall-clock `created_at` stamp.

    `created_at` is set via `datetime.now()` (see DropReceipt / _now_iso in
    __init__.py) and is EXPECTED to differ between two separate distill()
    calls — that's not nondeterminism, it's a timestamp doing its job.
    Asserting whole-receipt equality including it (the previous version of
    this test did) is flaky by construction: it fails whenever the two
    calls straddle a wall-clock second, which is exactly what happened when
    this file was run as part of the board-19 pass on 2026-08-14. That was
    a test bug, not a product bug — .content and every digest-bearing
    receipt field were byte-identical both times. Strip the timestamp
    before comparing so the check tests what it claims to test.
    """
    d = receipt.to_dict()
    d.pop("created_at", None)
    return d


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
    assert _stable_receipt(r1.receipt) == _stable_receipt(r2.receipt)
    print("PASS determinism: json strategy, same input -> identical output twice")


def test_determinism_tabular():
    r1 = distill(_big_rows(), budget=400)
    r2 = distill(_big_rows(), budget=400)
    assert json.dumps(r1.content, sort_keys=True) == json.dumps(r2.content, sort_keys=True)
    assert _stable_receipt(r1.receipt) == _stable_receipt(r2.receipt)
    print("PASS determinism: tabular strategy, same input -> identical output twice")


def test_determinism_text():
    text = _big_text()
    r1 = distill(text, budget=100, query="root cause")
    r2 = distill(text, budget=100, query="root cause")
    assert r1.content == r2.content
    assert _stable_receipt(r1.receipt) == _stable_receipt(r2.receipt)
    print("PASS determinism: text strategy, same input+query -> identical output twice")


def test_determinism_repeated_many_runs():
    data = _big_json()
    outs = {json.dumps(distill(data, budget=250).content, sort_keys=True) for _ in range(8)}
    assert len(outs) == 1, "8 runs of the same input produced different outputs"
    print("PASS determinism: 8 repeated runs collapse to one output")


# ---------------------------------------------------------------------------
# Cross-process determinism + cross-version golden-output guard (board 19).
#
# `golden_fixtures.json` holds four (input, budget, schema_hint, query)
# cases, one per strategy path (json / tabular-rows / tabular-csv-text /
# text), each frozen with the exact `distill()` output it produced at
# package version 0.1.0 (2026-08-14). It backs BOTH checks below:
#   - golden-output: proves TODAY's code produces the SAME output as the
#     frozen one, for unchanged input — the cross-version promise-keeper.
#   - cross-process: proves the SAME input distilled in two independent
#     python interpreters (not just two calls in this one process) produces
#     byte-identical stdout — rules out anything that could vary
#     process-to-process (e.g. PYTHONHASHSEED-driven ordering) that a
#     same-process check can't rule out.
# ---------------------------------------------------------------------------

_GOLDEN_FIXTURES_PATH = Path(__file__).resolve().parent / "golden_fixtures.json"
_GOLDEN_FIXTURES = json.loads(_GOLDEN_FIXTURES_PATH.read_text(encoding="utf-8"))

_CROSS_PROCESS_WORKER = r"""
import json, sys
from arcaeon_distill import distill

payload = json.loads(sys.stdin.read())
kwargs = {"budget": payload["budget"]}
if payload.get("schema_hint") is not None:
    kwargs["schema_hint"] = payload["schema_hint"]
if payload.get("query") is not None:
    kwargs["query"] = payload["query"]
result = distill(payload["input"], **kwargs)
receipt = result.receipt.to_dict() if result.receipt else None
if receipt is not None:
    receipt.pop("created_at", None)  # wall-clock, expected to differ -- see _stable_receipt
out = {"content": result.content, "strategy": result.strategy,
       "truncated": result.truncated, "receipt_stable": receipt}
sys.stdout.write(json.dumps(out, sort_keys=True, ensure_ascii=False))
"""


def _distill_in_subprocess(case: dict) -> str:
    """Runs distill() for `case` in a FRESH python interpreter (not this
    process) and returns its stdout verbatim."""
    payload = json.dumps({"input": case["input"], "budget": case["budget"],
                           "schema_hint": case["schema_hint"], "query": case["query"]})
    proc = subprocess.run(
        [sys.executable, "-c", _CROSS_PROCESS_WORKER],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).resolve().parent), timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"cross-process worker for {case['name']!r} failed: {proc.stderr}")
    return proc.stdout


def test_cross_process_determinism():
    for case in _GOLDEN_FIXTURES["cases"]:
        out1 = _distill_in_subprocess(case)
        out2 = _distill_in_subprocess(case)
        assert out1 == out2, (
            f"{case['name']}: same input distilled in two separate python "
            f"processes produced different bytes -- real nondeterminism")
    print(f"PASS cross-process determinism: {len(_GOLDEN_FIXTURES['cases'])} cases, "
          f"each run in two independent interpreters, byte-identical stdout")


def test_golden_output_matches_frozen_v0_1_0():
    for case in _GOLDEN_FIXTURES["cases"]:
        kwargs = {"budget": case["budget"]}
        if case["schema_hint"] is not None:
            kwargs["schema_hint"] = case["schema_hint"]
        if case["query"] is not None:
            kwargs["query"] = case["query"]
        result = distill(case["input"], **kwargs)
        assert result.content == case["golden_content"], (
            f"{case['name']}: output for this UNCHANGED input no longer matches "
            f"the frozen {_GOLDEN_FIXTURES['frozen_at_package_version']} golden "
            f"fixture -- distill() behavior changed for existing inputs")
        assert result.strategy == case["golden_strategy"]
        assert result.truncated == case["golden_truncated"]
    print(f"PASS golden output: {len(_GOLDEN_FIXTURES['cases'])} cases match the "
          f"frozen {_GOLDEN_FIXTURES['frozen_at_package_version']} fixtures byte-for-byte")


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
    assert result.receipt is not None  # receipt=True is distill()'s default
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
    assert result.receipt is not None  # receipt=True is distill()'s default
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
    assert result.receipt is not None  # receipt=True is distill()'s default
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
    assert result.receipt is not None  # receipt=True is distill()'s default
    assert result.receipt.drops[0]["kind"] == "rows_dropped"
    print("PASS tabular strategy (CSV text): header kept, dropped-row count in body")


def test_text_strategy_reassembles_in_original_order():
    text = _big_text()
    result = distill(text, budget=100, schema_hint="text", query="DNS root cause")
    assert result.strategy == "text"
    assert result.content.startswith("The outage began")
    assert "stale DNS record" in result.content or "Root cause" in result.content
    assert result.receipt is not None  # receipt=True is distill()'s default
    assert result.receipt.drops[0]["kind"] == "sentences_dropped"
    print("PASS text strategy: extractive selection keeps lead + query-relevant "
          "sentence, reassembled in original order")


def test_drop_receipt_catches_planted_inconsistency():
    # Plant the lie: claim truncated=False while a drop is recorded (or the
    # reverse). verify_receipt must catch the internal contradiction.
    honest = distill(_big_json(), budget=150).receipt
    assert honest is not None  # receipt=True is distill()'s default
    v_honest = verify_receipt(honest)
    assert v_honest["ok"], v_honest

    tampered = DropReceipt(**{**honest.to_dict(), "truncated": False})
    v_tampered = verify_receipt(tampered)
    assert not v_tampered["ok"]
    assert any("disagrees" in n for n in v_tampered["notes"]), v_tampered["notes"]
    print("PASS drop receipt: planted truncated/drops mismatch is caught by verify_receipt")


def test_drop_receipt_catches_corrupted_digest():
    receipt = distill(_big_rows(), budget=300).receipt
    assert receipt is not None  # receipt=True is distill()'s default
    row = receipt.to_dict()
    row["distilled"]["digest"] = "not-a-real-digest"
    v = verify_receipt(row)
    assert not v["ok"]
    assert any("well-formed" in n for n in v["notes"]), v["notes"]
    print("PASS drop receipt: malformed digest field is caught by verify_receipt")


def test_receipt_never_contains_full_content():
    result = distill(_big_json(), budget=150)
    assert result.receipt is not None  # receipt=True is distill()'s default
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
    assert result.receipt is not None  # receipt=True is distill()'s default
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
