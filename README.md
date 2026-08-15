# arcaeon-distill

<!-- mcp-name: io.arcaeon/distill -->

**A big tool output doesn't need to be a big tool output. `arcaeon-distill`
compacts it under a budget — deterministically, so it doesn't fight your
provider's prompt cache — and keeps a receipt for exactly what it cut.**

```
pip install arcaeon-distill     # then:  from arcaeon_distill import distill
```

```python
from arcaeon_distill import distill

def call_some_api():             # stand-in for your real tool call
    return {"items": [{"id": i, "sku": f"WID-{i:04d}", "in_stock": i % 3 == 0,
                       "description": "a durable stainless widget, " * 12}
                      for i in range(300)]}

huge = call_some_api()          # ~30k tokens of JSON, most of it noise
result = distill(huge, budget=500)

result.content          # the compacted structure — keys kept, values capped
result.receipt          # DropReceipt: prove what got cut, re-fetch if it mattered
print(result)           # "distilled via json: 29800 -> 200 est. tokens (~99% cut, budget 500, truncated=True)"
```

## Read this before the features: token reduction is not cost reduction

A July 2026 paper, **"Token Reduction Is Not Cost Reduction"** (arXiv
[2607.12161](https://arxiv.org/abs/2607.12161)), ran three token-reduction
approaches against an unmodified Claude Code baseline and found the
aggressive setup cut delivered tool-output tokens by **38.4%** while
**increasing billed cost by 6.8%.** Quoting the abstract directly:

> "The largest compression setup reduced delivered tool-output tokens by
> 38.4% but increased billed cost by 6.8%, while lighter compression produced
> only small and statistically uncertain savings. Across tasks, token
> reduction was weakly correlated with cost reduction (Pearson r = 0.15).
> Cost decomposition shows that prompt-cache creation and reads dominate the
> measured input-side cost... compression can alter agent trajectories
> through additional retrieval, diagnosis, testing, and turns, offsetting
> local token savings. On a SWE-bench Go subset, aggressive compression also
> reduced successful patch application."

Why: providers bill prompt-cache **writes and reads**, not raw token count.
A compressor whose output shifts from call to call — even for the *same*
underlying tool result — busts the cached prefix and forces a full
cache-write every time. Aggressive, unstable pruning also changes agent
trajectories: extra retrieval and diagnosis turns that eat the local token
savings, and in that paper's benchmark, sometimes broke the task outright.

**So this library makes no cost-savings claim.** It is positioned on
**context-budget headroom and task reliability** — fit more real signal in
the window, fewer truncation-driven failures — not on dollars. If you came
here for a "$ saved" number, that number is not one this library will give
you, because the paper above shows it usually isn't reliably true. What it
gives you instead: a smaller, **cache-stable** context footprint, and a
receipt that tells you when the compaction cut something that mattered.

## Deterministic, on purpose

The engineering constraint that flows straight from the finding above: **the
same input at the same budget produces byte-identical output, every run,
every machine.** No LLM on the hot path (nothing to be nondeterministic
about — a generative summarizer can't guarantee a byte-identical rewrite
and will paraphrase IDs into garbage anyway). No wall-clock, no randomness,
no unstable tie-breaking anywhere in the ranking or truncation logic. Call
`distill()` twice on the same tool output and a provider's prompt cache sees
the same bytes twice — not a new prefix to write and bill for.

**This property is regression-guarded, not just asserted.** `test_distill.py`
checks it three ways: same-process (repeated calls in one interpreter),
cross-process (the same input distilled in two independent python
interpreters, stdout diffed byte-for-byte — rules out anything that could
vary run-to-run, like hash-seed-driven ordering, that a same-process check
can't catch), and cross-version (`golden_fixtures.json` freezes the exact
output for four representative inputs at package version 0.1.0 — any future
code change that alters output for one of those unchanged inputs fails the
suite loudly, instead of silently shipping a cache-busting regression).

## Three strategies, picked from the input's shape

```python
distill(tool_output, budget=2000, schema_hint=None, query=None, receipt=True)
```

- **json** — dict/list input (or a str that parses as JSON). Every key of a
  **retained** value is kept — a dict/list element cut whole by truncation
  takes its keys with it, the same way a dropped row takes its columns.
  Long string values are truncated with a `"...+412 more chars"` count;
  long list values keep a head/tail slice with an `"...+412 more items"`
  marker where the middle used to be; a **wide dict** (many keys — an
  id→status map, a flat config) keeps a head/tail slice of *keys* the same
  way, with a `"__distilled_dropped_keys__"` count marker.
- **tabular** — list-of-dicts, list-of-lists, or CSV/TSV/markdown-table
  text. Keeps the header, a head slice and a tail slice of rows, and a
  dropped-row count between them.
- **text** — free text. **Deterministic extractive** sentence selection —
  score by position (lead + conclusion weighted over the middle) and, if you
  pass `query`, keyword overlap with it. **Not an LLM call.** Kept sentences
  are reassembled in their original order with a `[...]` gap marker, so the
  surviving text still reads as prose, not a shuffled highlight reel.

`schema_hint="json" | "tabular" | "text"` forces a strategy instead of
auto-detecting. `budget` is an approximate **token** budget (see
`estimate_tokens`, below — a heuristic, not a real tokenizer count).

## The honesty hook: the drop receipt

Deterministic extraction is not semantic understanding. Position+keyword
sentence ranking, head/tail slicing, and length-based truncation are
mechanical rules — they can, and sometimes will, cut the one line that
actually mattered. That's exactly why `distill()` doesn't just cut quietly:

```python
result = distill(incident_log, budget=200)
result.receipt.full        # {"digest": "sha256:...", "bytes": 41302}
result.receipt.distilled   # {"digest": "sha256:...", "bytes": 812}
result.receipt.drops
# [{"kind": "string_truncated", "path": "body",
#   "digest": "sha256:raw-bytes:v1:...", "dropped_bytes": 40100,
#   "dropped_count": 40100}, ...]
```

Each **per-drop** digest is of the dropped content only; the receipt also
carries a one-way digest of the full input (`full.digest`) and of the
distilled output (`distilled.digest`) — the `result.receipt.full` /
`.distilled` fields shown above. All are self-describing
(`sha256:<recipe>:<version>:<hex>`), compatible with
[`arcaeon-ledger`](https://pypi.org/project/arcaeon-ledger/)'s format so a
receipt travels cleanly into a chain, but `arcaeon_distill` never *requires*
`arcaeon-ledger` to be installed. No content — kept or cut — is ever carried
*verbatim*, so a receipt never reproduces the input. But these are **hashes,
not encryption**: a digest is a confirmation oracle. Anyone holding the
receipt can confirm a *guessed* value by re-hashing it, so any low-entropy
part of the input — a 4-digit code, a boolean, a value from a known small
set — is recoverable by brute force from `full.digest`, whether it was kept
or cut. The receipt is safe to log or ship when the input's unknown parts are
high-entropy; treat it as sensitive as the input itself when they are not.

```python
from arcaeon_distill import verify_receipt

verify_receipt(result.receipt)      # self-consistency: schema, digests
                                     # well-formed, truncated agrees with drops
```

If an agent reads a distilled result and something looks off — a field it
expected is missing, a count doesn't add up — the receipt's `full.digest`
lets it prove that *this* receipt describes the tool output it's holding,
and the drop manifest tells it exactly what to re-fetch. **"Distill, but
keep the receipt."** That's the differentiator: distillers that lose data
silently, versus one that's tamper-evidently honest about the loss.

Chain a receipt onto a tamper-evident ledger (optional — this is the only
place `arcaeon-ledger` is ever touched):

```bash
pip install "arcaeon-distill[ledger]"   # or: pip install arcaeon-ledger
```

```python
result.receipt.seal("receipts.jsonl", distiller="my-agent-v3")
# -> chained row, same tamper-evidence as any other arcaeon-ledger entry
```

## `estimate_tokens()` — cheap, and it says so

```python
from arcaeon_distill import estimate_tokens
estimate_tokens("some text")   # ~len(text) // 4
```

A heuristic, not a tokenizer call: no dependency, no model-specific
vocabulary. It will be wrong, sometimes by a lot, on code, non-English text,
and highly repetitive strings. Use it to size a budget cheaply — never to
predict a bill.

## Drop it into any MCP agent

```json
{
  "mcpServers": {
    "distill": {
      "command": "python",
      "args": ["-m", "arcaeon_distill.mcp_server"]
    }
  }
}
```

One tool, `distill_tool_output(tool_output, budget, schema_hint, query,
receipt)`, returning content + strategy + token estimates + the drop
receipt. Zero dependencies — MCP is JSON-RPC over stdio and this speaks it
directly, no SDK. Import-guarded: `arcaeon_distill` itself never imports
`mcp_server`, so `distill()` works with zero MCP awareness and the server is
only touched if you run it.

## What this does NOT do — read before you assume

Being precise about the boundary is the product, not a disclaimer.

**1. It does not guarantee cost reduction.** Covered at the top, and worth
repeating because it's the whole reason this library is shaped the way it
is: raw token count and billed cost are only weakly correlated under prompt
caching (arXiv 2607.12161 measured Pearson r = 0.15 across tasks). This
library's claim is context-budget headroom and reliability, never a dollar
figure — and it's built deterministic specifically so it doesn't accidentally
make the caching-cost problem worse.

**2. Deterministic extraction is not semantic understanding.** Nothing here
reads for meaning. Position+keyword sentence ranking and length-based value
truncation are mechanical rules that can drop the one fact that mattered —
which is exactly why the drop receipt exists. A pass is not a promise
nothing important was lost; it's a promise you can check.

**3. Budget is best-effort, not a hard cap on pathological input.**
`distill()` shrinks its internal caps across a bounded number of passes and
stops. Deeply nested structures, one enormous atomic value with no natural
cut point below the floor, or degenerate inputs can land over budget. It
will never loop forever chasing an unreachable target and it will never take
a different number of shrink passes on the same input twice — determinism
holds even when the budget isn't hit — but it does not promise the number
never overshoots.

## Complements, doesn't replace

- [`arcaeon-dedup`](https://pypi.org/project/arcaeon-dedup/) strips
  near-duplicate text across *multiple* tool outputs (SimHash, zero-dep).
  `arcaeon-distill` shrinks *one* tool output under a budget. Run dedup
  first across a batch, then distill what's left, and you've addressed both
  the "same thing twice" and the "one thing too big" failure modes.
- [`arcaeon-compact`](https://pypi.org/project/arcaeon-compact/) receipts
  *conversation/memory compaction* (an LLM or heuristic summarizer's claim
  about what it kept). `arcaeon-distill` receipts *single tool-call*
  extraction. Different layer, same honesty pattern, same digest format.
- [`arcaeon-ledger`](https://pypi.org/project/arcaeon-ledger/) is the
  tamper-evident chain either receipt can seal onto.

## Status

Pure stdlib (`json`, `hashlib`, `re`, `dataclasses`) — no required
dependencies. `arcaeon-ledger` is optional, only imported by
`DropReceipt.seal()`. Tested for determinism (repeated runs collapse to one
byte-identical output), budget adherence per strategy, and receipt honesty
(a planted claim mismatch — `truncated=True` with an empty drop list, or the
reverse — is caught by `verify_receipt`). Ships a runnable self-test with
frozen golden digest vectors:

```
python -m arcaeon_distill.selftest
```

MIT. Built by Arcaeon — the evidence layer for AI.
