# Changelog — arcaeon-distill

## 0.1.3 — 2026-08-16

Hypothesis property-test pass, same night as `arcaeon-continuity` and
`arcaeon-ledger` (ledger found a real `splitlines()`-vs-`ensure_ascii=False`
false-mismatch bug on the U+0085/U+2028/U+2029 unicode-line-separator class).
**Verdict for this package: that specific bug class is ABSENT.**
`arcaeon_distill` never writes a JSONL file and reads it back itself — the
digest functions use `ensure_ascii=False` but only ever feed `hashlib.sha256`,
and the text/tabular strategies already split on literal `"\n"`, not
`.splitlines()`. The one place this package DOES touch a real write-then-
read-back JSONL path is `DropReceipt.seal()` → `arcaeon_ledger.Ledger`, which
is covered end-to-end by a new integration property test
(`test_line_separator_class_survives_seal_and_ledger_readback`) and passes
against the already-fixed ledger 0.5.6.

Three real bugs turned up this pass and were fixed:

- **Fixed (H-int-1) — `DropReceipt.seal()` returned a receipt that silently
  diverged from what actually got chained.** `seal()` handed its row to
  `arcaeon_ledger.Ledger.append()`, which copies its input and
  `setdefault`-stamps `ts` on that COPY — so the dict `seal()` returned to
  the caller never had `ts` set, while the chained row did. A caller who
  re-hashed the receipt `seal()` gave them (the documented, honest way to
  verify their own copy) got a hash mismatch against the published chain —
  a false integrity failure with no tampering involved. Fixed by
  pre-stamping `ts` (same `"%Y-%m-%dT%H:%M:%SZ"` format as
  `arcaeon_ledger._now_iso()`) before calling `append()`, so `append()`'s
  `setdefault` becomes a no-op and the row `seal()` returns is now
  byte-identical to the row that was chained. Mirrors the identical fix
  already applied to `arcaeon_compact`'s `seal()` the same night.

- **Fixed (H-distill-1) — `distill(bytes_input, ...)` always raised, contradicting
  the documented contract.** The module docstring's Args section and
  `_reject_undistillable` both promise/admit raw `bytes` as a valid top-level
  input (`_digest_value` even has a dedicated bytes branch), but
  `_detect_strategy` had no bytes case and fell through to the generic
  `TypeError`. Fixed by decoding bytes to UTF-8 str up front in
  `_detect_strategy`, so bytes now flow through the same auto-detection (and
  `schema_hint`) logic as an equivalent str. Non-UTF-8 bytes raise a clear
  `ValueError` instead of a confusing downstream `TypeError`.

- **Fixed (H-distill-2) — the json-strategy shrink loop could make output
  LARGER than the input while reporting `truncated=True`.** With a budget too
  small to ever be reached (e.g. `budget=1` → 4 chars), the loop drives
  `list_cap`/`dict_cap` down to their floor (2 / 4) regardless of whether the
  container needed cutting at all. A 3-item list of empty lists (12 chars)
  got floor-capped to 2 items, inserting a `"...+1 more items"` marker (17
  chars) that is itself longer than the one item it replaced — net output
  grew from 12 to 28 chars while `truncated=True` claimed a cut helped.
  Found by `test_distill_content_is_a_json_fixpoint`. Fixed by tracking the
  smallest-size iteration seen across the shrink loop instead of
  unconditionally returning the last one; every iteration is an internally
  consistent candidate, so this is always at least as good and is a no-op on
  every case that already reaches budget (confirmed: all golden vectors in
  `selftest.py` unchanged; see the full test count below).

Also added, from the same mutation-testing pass, four boundary-condition
regression tests that caught surviving off-by-one mutants without finding a
live bug: a dict/list/string at exactly its default cap must round-trip
untruncated (`>` vs `>=` on the cap check), and a 2-row list-of-dicts — the
smallest valid input — must still auto-detect as the `tabular` strategy.

Test count: **32 → 51 passed** (37 in `test_distill.py` + 14 in
`test_hypothesis_distill.py`; all pre-existing tests still pass unmodified).

## 0.1.2 — 2026-08-15

Fixes from the 2026-08-15 adversarial scrutiny pass
(`projects/online_business/SCRUTINY_DISTILL_2026-08-15.md` in the Velouria repo).

- **Fixed (H2) — shared references were falsely refused as reference cycles.**
  `_reject_undistillable`'s admission walker tracked one global `seen` set of
  `id(v)` that was never popped on backtrack, so any value referenced twice
  (`x = [1,2,3]; {"a": x, "b": x}` — a legal DAG, `json.dumps` handles it
  fine) was refused with the message "input contains a reference cycle,"
  which was false. Rewrote the walker to track per-branch ancestors
  (`on_path`, popped via an explicit `_EXIT_MARKER` frame when a container's
  subtree finishes) separately from already-cleared shared subgraphs
  (`cleared`, skipped rather than re-walked). A true cycle — direct
  (`x.append(x)`) or indirect (`a["b"]=b; b["a"]=a`) — is still refused.
  Regression: `test_shared_reference_is_accepted_not_rejected_as_cycle`,
  `test_true_reference_cycle_is_still_rejected`.

- **Fixed (M1) — the json strategy never capped dict breadth; a wide dict
  blew the budget ~194x with `truncated=False`.** `_walk_json` capped string
  length and list length but had no dict-key cap, so a 5000-key dict at
  budget=100 (char_budget=400) landed at 19,445 chars — honest
  (`truncated=False`, nothing was cut) but misleading, since the README's
  disclosed over-budget exceptions didn't name this ordinary shape (a config,
  an id→status map, an embeddings dict). Added a `dict_cap` (default 200,
  floor 4) that shrinks alongside `str_cap`/`list_cap` in the same bounded
  shrink loop; a wide dict now keeps a head/tail slice of keys with a
  `__distilled_dropped_keys__` count marker and a `dict_truncated` drop entry,
  same pattern as `list_truncated`. The reproduction case now lands at 363–414
  chars against the 400-char budget (was 194.4x over) with `truncated=True`.
  Regression: `test_wide_dict_budget_is_enforced`.

- **Fixed (L1) — README/docstring "Every key is kept" overstated.** Reworded
  to "every key of a *retained* value is kept" in both the module docstring
  and README — a dict/list element cut whole by list or dict truncation takes
  its keys with it; that was always true and is now stated correctly.

- No schema change: `DropReceipt`/`verify_receipt` shape is unchanged, just a
  new `"dict_truncated"` value in the existing `drops[].kind` enum. Golden
  cross-version fixtures (`golden_fixtures.json`, frozen at 0.1.0) and
  selftest golden digest vectors are unaffected — none of the frozen inputs
  are wide enough to trigger the new dict cap (max dict width 4, cap default
  200) or share references. Full suite: 32/32 (`test_distill.py`), selftest
  `ALL PASSED`.

## 0.1.1 and earlier

Pre-CHANGELOG history. See git log and the 2026-08-14 audit for the
`schema_hint='tabular'` on a dict fix and the H1 receipt-privacy README/
docstring correction.
