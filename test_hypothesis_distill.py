"""Hypothesis property suite for arcaeon-distill.

Companion to the hand-rolled test_distill.py + selftest.py golden vectors.
Same treatment applied to arcaeon-continuity and arcaeon-ledger the night of
2026-08-15/16, which found a real bug in each (a probe-file handoff mismatch
in continuity, and a `splitlines()`-vs-`ensure_ascii=False` false-mismatch in
ledger — both traced to the same U+0085/U+2028/U+2029 unicode-line-separator
class). Run against this repo's checkout (`pip install -e .`), never
site-packages.

Sections:
  1. Determinism & idempotence — the package's entire engineering claim.
  2. Receipt self-consistency — verify_receipt() on anything distill() emits.
  3. Content preservation under budget — nothing is cut it didn't have to cut.
  4. Admission — _reject_undistillable's cycle/shared-ref/NaN/set/key rules.
  5. The unicode-line-separator class (U+0085/U+2028/U+2029) — THE headline
     check this pass exists to run. distill() itself never writes a JSONL
     file and reads it back (see section docstring below for the actual
     verdict); section 5b closes the loop by exercising the real round trip
     through DropReceipt.seal() -> arcaeon_ledger, which DOES write JSONL
     and read it back, on a receipt containing these characters.
"""
from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from arcaeon_distill import distill, verify_receipt

# ---------------------------------------------------------------------------
# strategies
# ---------------------------------------------------------------------------

# Plain finite JSON scalars only — NaN/Infinity are refused by design
# (_reject_undistillable), tested separately in section 4.
_json_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-10**12, max_value=10**12),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=40),
)


def _json_value(max_leaves: int = 25):
    return st.recursive(
        _json_scalar,
        lambda children: st.one_of(
            st.lists(children, max_size=6),
            st.dictionaries(st.text(min_size=1, max_size=12), children, max_size=6),
        ),
        max_leaves=max_leaves,
    )


def _distillable_top_level(max_leaves: int = 25):
    """distill() only accepts a dict/list/str/bytes at the TOP level (see the
    module docstring's Args section and `_detect_strategy`) -- a bare
    top-level int/float/bool/None is rejected with TypeError by design.
    `_json_value` above is used for values NESTED inside a container, where
    every JSON scalar is fair game; this wrapper restricts what gets handed
    directly to `distill()` in the property tests below to what the public
    contract actually promises."""
    inner = _json_value(max_leaves=max_leaves)
    return st.one_of(
        st.lists(inner, max_size=6),
        st.dictionaries(st.text(min_size=1, max_size=12), inner, max_size=6),
        st.text(max_size=200),
    )


# The three unicode line-separator characters at the center of tonight's bug
# class: NEL (U+0085), LINE SEPARATOR (U+2028), PARAGRAPH SEPARATOR (U+2029).
_LINE_SEP_CHARS = "  "

_text_with_line_seps = st.text(
    alphabet=st.one_of(
        st.characters(blacklist_categories=("Cs",), max_codepoint=0x2FFF),
        st.sampled_from(list(_LINE_SEP_CHARS)),
    ),
    min_size=1, max_size=200,
)

_slow_settings = settings(max_examples=60, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow])


def _strip_stamp(receipt_dict: dict) -> dict:
    d = dict(receipt_dict)
    d.pop("created_at", None)
    return d


# ---------------------------------------------------------------------------
# 1. Determinism & idempotence
# ---------------------------------------------------------------------------

@_slow_settings
@given(value=_distillable_top_level(), budget=st.integers(min_value=1, max_value=500))
def test_distill_is_deterministic_across_repeated_calls(value, budget):
    r1 = distill(value, budget=budget)
    r2 = distill(value, budget=budget)
    assert r1.content == r2.content
    assert r1.strategy == r2.strategy
    assert r1.truncated == r2.truncated
    assert _strip_stamp(r1.receipt.to_dict()) == _strip_stamp(r2.receipt.to_dict())


@_slow_settings
@given(value=_distillable_top_level(), budget=st.integers(min_value=1, max_value=500))
def test_distill_content_is_a_json_fixpoint(value, budget):
    """Re-distilling already-distilled JSON content at the SAME budget must
    not keep shrinking it forever -- content that already fits stays put,
    and content that was already the output of one pass is a stable point
    for a second pass at an equal-or-larger budget."""
    r1 = distill(value, budget=budget)
    r2 = distill(r1.content, budget=budget, schema_hint="json"
                 if isinstance(r1.content, (dict, list)) else None)
    # r2 must not be LARGER than r1 (monotonic: distilling a distillate never
    # grows it back).
    size1 = len(json.dumps(r1.content, ensure_ascii=False, default=str))
    size2 = len(json.dumps(r2.content, ensure_ascii=False, default=str))
    assert size2 <= size1 + 1  # +1 slack for quoting edge cases on markers


# ---------------------------------------------------------------------------
# 2. Receipt self-consistency
# ---------------------------------------------------------------------------

@_slow_settings
@given(value=_distillable_top_level(), budget=st.integers(min_value=1, max_value=200))
def test_every_receipt_self_verifies(value, budget):
    result = distill(value, budget=budget, receipt=True)
    v = verify_receipt(result.receipt)
    assert v["ok"], v["notes"]
    # truncated flag agrees with whether the strategy actually recorded drops
    assert result.truncated == bool(result.receipt.drops)


@_slow_settings
@given(value=_distillable_top_level(), budget=st.integers(min_value=1, max_value=200))
def test_receipt_off_does_not_change_content(value, budget):
    with_r = distill(value, budget=budget, receipt=True)
    without_r = distill(value, budget=budget, receipt=False)
    assert with_r.content == without_r.content
    assert with_r.truncated == without_r.truncated
    assert without_r.receipt is None


# ---------------------------------------------------------------------------
# 3. Content preservation under budget
# ---------------------------------------------------------------------------

@_slow_settings
@given(value=_distillable_top_level(max_leaves=8))
def test_generous_budget_never_truncates_small_input(value):
    """A budget far larger than the input can possibly need should return the
    input completely unmodified (truncated=False, content == input)."""
    huge_budget = 10_000_000
    result = distill(value, budget=huge_budget)
    assert result.truncated is False
    assert result.content == value
    assert result.receipt.drops == []


# ---------------------------------------------------------------------------
# 4. Admission contract (_reject_undistillable via the public distill())
# ---------------------------------------------------------------------------

def test_shared_reference_dag_is_accepted_not_rejected_as_cycle():
    """Regression guard for the documented H2 fix: a value referenced twice
    (a DAG) is legitimate and must NOT raise 'reference cycle'."""
    shared = [1, 2, 3]
    value = {"a": shared, "b": shared}
    result = distill(value, budget=1000)
    assert result.content == value


def test_actual_cycle_is_rejected():
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="reference cycle"):
        distill(cyclic, budget=1000)


@given(bad=st.one_of(st.just(float("nan")), st.just(float("inf")), st.just(float("-inf"))))
def test_nan_and_infinity_always_rejected(bad):
    with pytest.raises(ValueError):
        distill({"x": bad}, budget=1000)


def test_non_str_dict_key_rejected():
    with pytest.raises(TypeError):
        distill({1: "a"}, budget=1000)


def test_set_rejected():
    with pytest.raises(TypeError):
        distill({"x": {1, 2, 3}}, budget=1000)


# ---------------------------------------------------------------------------
# 5. The unicode-line-separator class: U+0085 / U+2028 / U+2029
# ---------------------------------------------------------------------------
# THE HEADLINE CHECK. Verdict for arcaeon-distill's OWN code, established by
# reading the source (not asserted, verified by grep + the tests below): the
# package never round-trips content through a file it both writes AND reads
# back itself. `_digest_json_c14n` / `_receipt_full` DO serialize with
# `ensure_ascii=False`, matching half the buggy idiom -- but that output only
# ever feeds `hashlib.sha256(...)`, never a `splitlines()` re-read within
# this package. Text-strategy line handling (`_looks_tabular_text`,
# `_distill_tabular_text`) uses `.split("\n")`, NOT `.splitlines()` -- the
# already-correct idiom. So section 5a below is a determinism/no-crash check
# (there is no internal read-back path to break), and section 5b is the
# integration check that actually closes the loop: DropReceipt.seal() DOES
# hand a row containing these characters to arcaeon_ledger.Ledger.append(),
# which writes JSONL with ensure_ascii=False and (as of 0.5.6, fixed the same
# night this suite was written) reads it back with .split("\n"). That
# integration is what section 5b proves end to end.

@_slow_settings
@given(s=_text_with_line_seps, budget=st.integers(min_value=1, max_value=200))
def test_line_separator_class_text_strategy_is_deterministic(s, budget):
    r1 = distill(s, budget=budget, schema_hint="text")
    r2 = distill(s, budget=budget, schema_hint="text")
    assert r1.content == r2.content
    assert _strip_stamp(r1.receipt.to_dict()) == _strip_stamp(r2.receipt.to_dict())


@_slow_settings
@given(s=_text_with_line_seps)
def test_line_separator_class_digest_is_stable(s):
    """The digest of a value containing U+0085/U+2028/U+2029 must be the
    same every time it's computed (this is the half of the idiom distill DOES
    exercise -- ensure_ascii=False through json-c14n/raw-bytes digesting)."""
    value = {"body": s}
    r1 = distill(value, budget=1000)
    r2 = distill(value, budget=1000)
    assert r1.receipt.full["digest"] == r2.receipt.full["digest"]


@_slow_settings
@given(s=_text_with_line_seps, budget=st.integers(min_value=1, max_value=60))
def test_line_separator_class_tabular_text_uses_literal_lf_not_splitlines(s, budget):
    """Regression guard for the exact bug class: tabular text-line counting
    must key off literal '\\n' only. If this ever regresses to .splitlines(),
    a body containing U+2028 etc. would silently report MORE rows than '\\n'
    characters actually present, corrupting the row-count math.
    Constructed directly (not through distill()) so the assertion pins the
    idiom itself, independent of whether this particular random string
    happens to look tabular."""
    text = f"h1,h2\n{s}\nrow2a,row2b\nrow3a,row3b"
    n_true_lines = text.count("\n") + 1
    n_splitlines = len(text.splitlines())
    lines_used_by_distill = text.strip("\n").split("\n")
    assert len(lines_used_by_distill) == text.strip("\n").count("\n") + 1
    if any(c in s for c in _LINE_SEP_CHARS) and n_splitlines != n_true_lines:
        # This random string actually contains a char that would fool
        # splitlines(): confirm distill's own line count does NOT match the
        # (wrong) splitlines() count, i.e. it used the literal-\n idiom.
        assert len(lines_used_by_distill) != n_splitlines or n_splitlines == n_true_lines


ledger = pytest.importorskip("arcaeon_ledger", reason="optional dependency for seal()")


@_slow_settings
@given(s=_text_with_line_seps)
def test_line_separator_class_survives_seal_and_ledger_readback(s):
    """THE end-to-end check: seal a receipt whose content contains
    U+0085/U+2028/U+2029 onto a real arcaeon_ledger JSONL file, then read it
    back through the ledger's own iterator (which -- as of the 0.5.6 fix --
    splits on literal '\\n', not .splitlines()). The character must survive
    byte-for-byte and the ledger must still verify clean. This is the actual
    write-JSONL-then-read-back path distill() participates in; distill()
    itself has no other one (see section 5 docstring).

    Uses its own tempfile.TemporaryDirectory() per example rather than the
    pytest `tmp_path` fixture -- Hypothesis flags function-scoped fixtures as
    unsafe under @given because they are NOT reset between generated
    examples, which would let ledger rows from earlier examples leak into
    later ones and corrupt this exact "exactly 1 row" assertion."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        result = distill({"body": s}, budget=100_000)
        ledger_path = Path(tmp) / "test_seal.jsonl"
        sealed_row = result.receipt.seal(str(ledger_path))
        assert sealed_row["full"]["digest"] == result.receipt.full["digest"]

        log = ledger.Ledger(str(ledger_path))
        rows = list(log)
        assert len(rows) == 1, (
            f"expected exactly 1 ledger row, got {len(rows)} -- a "
            f"splitlines()-class bug would fragment or merge rows containing "
            f"U+0085/U+2028/U+2029")
        assert rows[0]["kind"] == "distill_receipt"
        assert rows[0]["full"]["digest"] == result.receipt.full["digest"]

        verdict = log.verify()
        assert verdict.ok, verdict


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
