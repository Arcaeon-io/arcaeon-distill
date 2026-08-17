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
# sort_keys=False deliberately: dict KEY ORDER is part of the bytes an LLM and
# a prefix cache see, and it is the one thing PYTHONHASHSEED could plausibly
# move. sort_keys=True erased exactly what this guard exists to pin. Writing
# encoded bytes (not text) keeps a non-ASCII fixture from dying on a cp1252
# console.
sys.stdout.buffer.write(json.dumps(out, sort_keys=False,
                                   ensure_ascii=False).encode("utf-8"))
"""


def _distill_in_subprocess(case: dict) -> str:
    """Runs distill() for `case` in a FRESH python interpreter (not this
    process) and returns its stdout verbatim."""
    payload = json.dumps({"input": case["input"], "budget": case["budget"],
                           "schema_hint": case["schema_hint"], "query": case["query"]})
    proc = subprocess.run(
        [sys.executable, "-c", _CROSS_PROCESS_WORKER],
        input=payload.encode("utf-8"), capture_output=True,
        cwd=str(Path(__file__).resolve().parent), timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"cross-process worker for {case['name']!r} failed: "
                            f"{proc.stderr.decode('utf-8', 'replace')}")
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
        # Compare SERIALIZED bytes, not the objects: `{"a":1,"b":2} ==
        # {"b":2,"a":1}` is True in Python and False for every cache in front
        # of this. The order-blind comparison would have passed a total
        # rewrite of emitted key order.
        got = json.dumps(result.content, ensure_ascii=False, separators=(",", ":"),
                         sort_keys=False)
        want = json.dumps(case["golden_content"], ensure_ascii=False,
                          separators=(",", ":"), sort_keys=False)
        assert got == want, (
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


# ---------------------------------------------------------------------------
# Input admission (audit 2026-08-14).
#
# The cache-stability claim is the product. Three input shapes broke it and no
# downstream code path could repair them, so they are refused at the door. The
# nastiest part: the cross-process guard above cannot SEE any of them, because
# its transport is JSON and a set, a heap object, and a non-str dict key all
# fail to cross a JSON pipe. A live hash seed with nothing to test.
# ---------------------------------------------------------------------------

_ADVERSARIAL_WORKER = r"""
import sys
from arcaeon_distill import distill
payload = {"a" * 10: "x" * 200, "b" * 10: "y" * 200, "zz": {"n": 1, "m": 2}}
r = distill(payload, budget=60, receipt=True)
import json
sys.stdout.buffer.write(json.dumps(
    {"c": r.content, "d": r.receipt.to_dict()["full"]["digest"]},
    sort_keys=False, ensure_ascii=False).encode("utf-8"))
"""


def test_arbitrary_object_is_refused_not_stringified():
    """`str(obj)` embeds a heap address, so the documented "anything else is
    stringified" path violated byte-identity on EVERY run (ASLR), not just
    across machines."""
    class ApiResponse:
        def __init__(self):
            self.rows = list(range(50))

    for value in (ApiResponse(), object(), lambda x: x):
        try:
            distill(value, budget=200, receipt=False)
            assert False, f"{type(value).__name__} was accepted"
        except TypeError as e:
            assert "deterministic" in str(e)
    print("PASS an arbitrary object is a typed refusal, not a heap address in the output")


def test_sets_are_refused():
    """A set iterates in PYTHONHASHSEED order -- the textbook cross-process
    nondeterminism, and unreachable by a JSON-transport guard."""
    for value in ({"a", "b", "c"}, frozenset({"a", "b"}),
                  {"rows": [{"tags": {"x", "y"}}]}):
        try:
            distill(value, budget=200, receipt=False)
            assert False, f"{value!r} was accepted"
        except TypeError as e:
            assert "PYTHONHASHSEED" in str(e)
    print("PASS set/frozenset input is refused, nested ones too")


def test_non_string_dict_keys_are_refused():
    """json.dumps coerces non-str keys, so {1: v} and {"1": v} -- unequal
    inputs -- produced the SAME receipt digest. A digest that can't tell two
    inputs apart cannot prove which output it describes."""
    for value in ({1: "a" * 400}, {True: "x"}, {1.0: "x"},
                  {"ok": {2: "nested"}}):
        try:
            distill(value, budget=100)
            assert False, f"{value!r} was accepted"
        except TypeError as e:
            assert "not str" in str(e)
    # and the collision it prevented is real, so pin the pair explicitly
    a, b = {1: "a" * 400}, {"1": "a" * 400}
    assert a != b
    print("PASS non-str dict keys are refused (they collided two unequal inputs "
          "onto one digest)")


def test_nan_and_infinity_are_refused_consistently():
    """The package disagreed with itself: receipt=True raised out of `json`,
    receipt=False emitted the literals NaN/Infinity -- which are not JSON --
    straight into an agent's context."""
    for value in ({"x": float("nan"), "pad": "p" * 400},
                  {"x": float("inf"), "pad": "p" * 400},
                  [float("-inf")]):
        for receipt in (True, False):
            try:
                distill(value, budget=50, receipt=receipt)
                assert False, f"{value!r} accepted with receipt={receipt}"
            except ValueError as e:
                assert "NaN/Infinity" in str(e)
    print("PASS NaN/Infinity refused identically with and without a receipt")


def test_common_non_json_types_fail_typed():
    import datetime as _dt
    import decimal
    for value in (_dt.datetime(2026, 8, 14), decimal.Decimal("1.5")):
        try:
            distill(value, budget=100)
            assert False, f"{type(value).__name__} accepted"
        except TypeError as e:
            assert "deterministic serialization" in str(e)
    print("PASS datetime/Decimal fail with a distill-level TypeError, not one "
          "from inside json.encoder")


def test_tabular_hint_on_a_dict_does_not_silently_distill_the_keys():
    """`list(a_dict)` yields KEYS. schema_hint='tabular' on a dict threw away
    every value, reported truncated=False with an empty drop list, and
    verify_receipt certified it ok. Affirmatively certified total data loss."""
    payload = {"rows": [{"id": 1}], "meta": {"page": 1}}
    try:
        distill(payload, budget=100, schema_hint="tabular")
        assert False, "a dict was accepted as tabular"
    except TypeError as e:
        assert "list of row dicts" in str(e)
    for bad in (12345, "x"):
        try:
            distill(bad, budget=100, schema_hint="tabular")
        except (TypeError, ValueError):
            pass
    print("PASS schema_hint='tabular' on a dict raises instead of distilling the key names")


def test_shared_reference_is_accepted_not_rejected_as_cycle():
    """H2 (scrutiny 2026-08-15): a value referenced twice is a DAG, not a
    cycle. json.dumps handles it fine; the old admission walker's global
    `seen` set (never popped on backtrack) refused it as "a reference
    cycle" -- a false message on valid, JSON-serializable input."""
    shared = [1, 2, 3]
    data = {"a": shared, "b": shared}
    result = distill(data, budget=200)
    assert result.content == {"a": [1, 2, 3], "b": [1, 2, 3]}
    assert result.receipt is not None  # receipt=True is distill()'s default
    v = verify_receipt(result.receipt)
    assert v["ok"], v
    # a value shared three ways, and nested, still isn't a cycle
    nested_shared = {"x": 1}
    data2 = {"a": [nested_shared, nested_shared], "b": nested_shared}
    result2 = distill(data2, budget=200)
    assert result2.content == {"a": [{"x": 1}, {"x": 1}], "b": {"x": 1}}
    print("PASS shared reference (DAG) is accepted, not refused as a cycle")


def test_true_reference_cycle_is_still_rejected():
    """The H2 fix must not turn off real-cycle detection. A container that
    contains itself (directly or through one hop) still raises -- otherwise
    `_walk_json` would recurse it to a RecursionError instead of a clean,
    typed refusal at the door."""
    self_referencing_list: list = []
    self_referencing_list.append(self_referencing_list)
    try:
        distill(self_referencing_list, budget=200)
        assert False, "a list containing itself was accepted"
    except ValueError as e:
        assert "reference cycle" in str(e)

    a: dict = {}
    b = {"a": a}
    a["b"] = b
    try:
        distill(a, budget=200)
        assert False, "an indirect a->b->a cycle was accepted"
    except ValueError as e:
        assert "reference cycle" in str(e)
    print("PASS a real reference cycle (direct and indirect) is still refused")


def test_wide_dict_budget_is_enforced():
    """M1 (scrutiny 2026-08-15): the json strategy capped string length and
    list length but never dict BREADTH, so a wide dict (many keys -- an
    id->status map, a flat config) blew the budget ~194x with
    truncated=False (honest -- nothing was cut -- but misleading, since
    ordinary-shaped input was expected to land near budget). A dict-key cap
    now shrinks alongside str_cap/list_cap; the drop is recorded like any
    other, not silently absorbed."""
    data = {f"key_{i}": i for i in range(5000)}
    result = distill(data, budget=100)  # char_budget = 400
    est_size = len(json.dumps(result.content, ensure_ascii=False, separators=(",", ":")))
    assert result.truncated
    assert est_size < 4 * 400, (  # was ~194x over; must now be in the same ballpark as budget
        f"wide dict still blew the budget: {est_size} chars vs a 400-char budget")
    assert result.receipt is not None
    kinds = {d["kind"] for d in result.receipt.drops}
    assert "dict_truncated" in kinds
    assert "__distilled_dropped_keys__" in result.content
    v = verify_receipt(result.receipt)
    assert v["ok"], v
    print(f"PASS wide dict (5000 keys, budget 100) now truncates: "
          f"{est_size} chars (was 194.4x over budget pre-fix), truncated=True, "
          f"dict_truncated recorded, verify_receipt ok")


def test_dict_at_exact_cap_is_not_truncated():
    """Mutation-testing find (2026-08-16): `_walk_json`'s dict-breadth check
    is `len(obj) > dict_cap` (default 200). An off-by-one mutant (`>=`)
    SURVIVED the full suite -- nothing exercised a dict with EXACTLY
    `dict_cap` keys, so the boundary itself was unverified. A dict of
    exactly 200 keys must round-trip whole: no `__distilled_dropped_keys__`
    marker, no `dict_truncated` drop, `truncated=False` -- the cap is a
    "more than this shrinks" line, not "this much or more shrinks"."""
    data = {f"k{i}": i for i in range(200)}  # == default dict_cap, not over it
    result = distill(data, budget=5000)  # generous budget: no shrink-loop interference
    assert result.strategy == "json"
    assert not result.truncated, "a dict of exactly dict_cap keys was truncated"
    assert "__distilled_dropped_keys__" not in result.content
    assert len(result.content) == 200
    assert result.content == data
    print("PASS a 200-key dict (== default dict_cap) survives whole, untruncated")


def test_list_at_exact_cap_is_not_truncated():
    """Mutation-testing find (2026-08-16): same off-by-one shape as the dict
    cap, on `_walk_json`'s list-length check (`> list_cap`, default 20). A
    `>=` mutant SURVIVED -- no test used a list of exactly `list_cap` items.
    At exactly 20 items nothing should be cut: no "...+N more items"
    marker, no `list_truncated` drop."""
    data = list(range(20))  # == default list_cap, not over it
    result = distill(data, budget=5000, schema_hint="json")
    assert not result.truncated, "a list of exactly list_cap items was truncated"
    assert result.content == data
    assert not any(isinstance(x, str) and x.startswith("...+") for x in result.content)
    print("PASS a 20-item list (== default list_cap) survives whole, untruncated")


def test_string_at_exact_cap_is_unchanged():
    """Mutation-testing find (2026-08-16): same off-by-one shape again on the
    string cap (`> str_cap`, default 300). The `>=` mutant is worse than the
    dict/list cases -- at len(s) == str_cap, `cut = obj[str_cap:]` is EMPTY,
    so the mutant appends a live '...+0 more chars' suffix to an
    already-complete string, making the 'distilled' output LONGER than the
    original while claiming a drop happened. A 300-char string must come
    back byte-identical, not with a phantom marker for zero dropped chars."""
    s = "x" * 300  # == default str_cap, not over it
    result = distill({"s": s}, budget=5000)
    assert not result.truncated, "a string of exactly str_cap chars was truncated"
    assert result.content["s"] == s, (
        f"300-char string round-tripped as {result.content['s']!r} "
        f"({len(result.content['s'])} chars) -- expected byte-identical")
    print("PASS a 300-char string (== default str_cap) round-trips unchanged")


def test_two_row_list_of_dicts_is_detected_as_tabular():
    """Mutation-testing find (2026-08-16): `_is_row_list`'s length guard is
    `len(data) < 2` (the tabular strategy needs at least a header shape, so
    a 0- or 1-row list falls back to json). An off-by-one mutant (`<= 2`)
    SURVIVED -- no test checked strategy auto-detection on the SMALLEST
    valid tabular input, exactly 2 rows. Under the mutant, a 2-row
    list-of-dicts silently detects as "json" instead of "tabular" -- still
    correct output, but the wrong strategy label and the wrong receipt
    shape (dict/list drops instead of rows_dropped) for any caller that
    branches on `result.strategy`."""
    rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]  # smallest valid tabular input
    result = distill(rows, budget=5000)  # no schema_hint: exercise auto-detect
    assert result.strategy == "tabular", (
        f"a 2-row list-of-dicts auto-detected as {result.strategy!r}, not 'tabular'")
    print("PASS a 2-row list-of-dicts auto-detects as the tabular strategy")


def test_cross_process_determinism_on_an_adversarial_payload():
    """The JSON-transport guard can't express a hostile payload, so build one
    INSIDE the worker: long equal-length values (ranking ties), nested dicts
    (key order), and two runs under different explicit hash seeds."""
    import os
    outs = []
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        proc = subprocess.run(
            [sys.executable, "-c", _ADVERSARIAL_WORKER], capture_output=True,
            cwd=str(Path(__file__).resolve().parent), timeout=30, env=env)
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        outs.append(proc.stdout)
    assert len(set(outs)) == 1, (
        "the same payload distilled under PYTHONHASHSEED 0/1/12345 produced "
        "different bytes -- real cross-process nondeterminism")
    print("PASS adversarial payload is byte-identical under three explicit hash seeds")


def test_json_shrink_loop_stops_at_exact_budget_not_past_it():
    """Mutation-testing find (2026-08-16): the shrink-loop stop condition in
    `_distill_json` is `size <= char_budget` -- inclusive, so a pass that
    lands EXACTLY on budget stops right there instead of shrinking again for
    no reason. A `<=` -> `<` mutant SURVIVED the whole suite because no
    existing case makes a shrink-loop iteration's serialized size land on
    char_budget exactly; that requires solving for the budget from the
    output size, not guessing one.

    A dict with a single 301-char string value truncates, at the DEFAULT
    str_cap=300, to a 300-char string plus a "...+1 more chars" marker; the
    whole distilled JSON blob is exactly 324 chars, which is char_budget for
    budget=81 tokens (81*4=324) -- the *first* shrink-loop pass already
    lands on budget to the byte.

    Correct code stops there (a 1-char drop). A `<` mutant sees
    `324 < 324` is False, does not stop, halves the caps (str_cap 300->150),
    re-walks, and that deeper pass becomes the new smallest-seen result: 151
    chars dropped instead of 1, at the identical budget -- needless
    truncation the exact-fit pass never required."""
    data = {"s": "x" * 301}
    r = distill(data, budget=81, receipt=False)
    size = len(json.dumps(r.content, ensure_ascii=False, separators=(",", ":")))
    assert size == 324, "fixture drifted: exact-budget size changed (%d)" % size
    assert r.content["s"] == "x" * 300 + "...+1 more chars", (
        "shrink loop over-truncated past the exact-fit first pass: %r"
        % r.content["s"])
    print("PASS shrink-loop stop is <= (inclusive): an exact-budget fit "
          "is not shrunk further")


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
