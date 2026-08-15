# Changelog — arcaeon-distill

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
