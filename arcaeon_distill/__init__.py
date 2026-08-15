"""arcaeon_distill — a deterministic tool-output distiller for AI agents.

Tool calls come back huge: a 50k-token API dump, a 900-row query result, a
search response nobody asked to read in full. `distill()` compacts it under a
budget, **the same way every time** — same input, same budget, byte-identical
output, every run, every machine. That determinism is not a nicety here, it
is the entire engineering point (read "Why deterministic," below, before the
feature list).

    from arcaeon_distill import distill

    result = distill(big_json_blob, budget=500)
    result.content          # the compacted structure, keys kept, values capped
    result.receipt          # a DropReceipt: prove what got cut, and by how much

Three built-in strategies, picked automatically from the input's shape (or
forced with `schema_hint`):

  - **json**    dict/list input (or a str that parses as JSON). Every key is
                kept; long string values are truncated with a "+N more chars"
                count; long list values keep a head/tail slice with a
                "...+N more items" marker in place of the middle.
  - **tabular** list-of-dicts, list-of-lists, or CSV/TSV/markdown-table text.
                Keeps the header, a head slice and a tail slice of rows, and
                a dropped-row count in between.
  - **text**    free text. Deterministic EXTRACTIVE sentence selection —
                score by position (lead + conclusion weighted) and, if a
                `query` is given, keyword overlap with it — not an LLM call.
                Kept sentences are reassembled in their ORIGINAL order with a
                gap marker where sentences were cut, so the surviving text
                still reads as prose, not a scrambled highlight reel.

Why deterministic (read this before anything else)
----------------------------------------------------
A July 2026 paper, "Token Reduction Is Not Cost Reduction" (arXiv 2607.12161),
measured three token-reduction approaches against an unmodified Claude Code
baseline and found the aggressive setup cut delivered tool-output tokens by
38.4% while INCREASING billed cost by 6.8%. The mechanism: providers bill
prompt-cache creates and reads, not raw token count, and a compressor whose
output shifts from call to call — even for the *same* underlying tool result
— invalidates the cached prefix and forces a full cache-write on every turn.
Aggressive, non-deterministic pruning also changed agent trajectories:
extra retrieval/diagnosis/testing turns that ate the local token savings, and
on one benchmark subset, aggressive compression *reduced* successful task
completion.

So `distill()` is built around one hard constraint: **the same input at the
same budget always produces byte-identical output.** No LLM on the hot path
(nothing to be nondeterministic about), no wall-clock or random tie-breaking
anywhere in the ranking or truncation logic, no dict-order dependence beyond
what the input itself already fixes. This does not make distillation free —
a shorter tool result is still a shorter prefix than the un-distilled one,
which is its own cache consideration — but it makes distillation *cache-
stable*: call it on the same tool output twice and the provider's cache sees
the same bytes twice, not a new prefix to bill for.

This library makes NO cost-savings claim. It is positioned on context-budget
headroom and task reliability (fitting more real signal in the window, fewer
truncation-driven failures) — read the non-proofs below and in the README
before assuming "fewer tokens" means "cheaper."

The honesty hook: the drop receipt
-----------------------------------
Deterministic extraction is not semantic understanding — it can drop the one
line that mattered. That is exactly why every `distill()` call can produce a
`DropReceipt`: a digest of the full input, a digest of the distilled output,
and a manifest of exactly what got cut (by path/location, byte count, and a
digest of the cut content) — so a receiving agent can look at the receipt,
decide the drop looks load-bearing, and go re-fetch the original. A silent
distiller loses data quietly; this one is tamper-evidently honest that it
lost anything at all. "Distill, but keep the receipt."

Non-proofs — read before the features
--------------------------------------
1. Does NOT guarantee cost reduction. See "Why deterministic," above — token
   count and billed cost are only weakly correlated under prompt caching
   (arXiv 2607.12161 measured Pearson r = 0.15). This library's claim is
   context-budget headroom and reliability, never a dollar figure.
2. Deterministic extraction is not semantic understanding. Position+keyword
   sentence ranking, head/tail row slicing, and length-based value truncation
   are mechanical rules. They will, sometimes, cut the one fact that mattered
   — the drop receipt exists because that failure mode is real, not
   hypothetical, and the honest fix is provable recovery, not a promise it
   won't happen.
3. Budget is best-effort, not a hard cap on pathological input. `distill()`
   shrinks its internal caps across a bounded number of passes and stops;
   deeply nested structures, single enormous atomic values (one 10MB string
   with no natural cut point below the minimum floor), or degenerate inputs
   can land over budget. It will never loop forever chasing an unreachable
   target, and it will never re-run the shrink loop a different number of
   times for the same input (determinism holds even when the budget isn't
   hit) — but it does not promise the number never overshoots.

Stdlib only. `arcaeon_ledger` is an optional dependency, imported only if you
call `DropReceipt.seal()` to chain a receipt onto a tamper-evident ledger.
MIT.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

__version__ = "0.1.0"
__all__ = [
    "distill", "estimate_tokens", "DistilledResult", "DropReceipt",
    "verify_receipt", "SCHEMA",
]

SCHEMA = "arcaeon-distill:receipt:v1"

# ---------------------------------------------------------------------------
# digests — self-describing, compatible with arcaeon-ledger's format
# (`sha256:<recipe>:<version>:<hex>`) so a receipt travels cleanly into an
# arcaeon-ledger/arcaeon-compact chain, but arcaeon_distill never REQUIRES
# arcaeon_ledger to be installed: it falls back to computing the identical
# format itself.
# ---------------------------------------------------------------------------

def _digest_bytes_raw(b: bytes) -> str:
    try:
        from arcaeon_ledger import digest_bytes  # type: ignore
        return digest_bytes(b)
    except ImportError:
        return "sha256:raw-bytes:v1:" + hashlib.sha256(b).hexdigest()


def _digest_json_c14n(value: Any) -> str:
    try:
        from arcaeon_ledger import digest_json  # type: ignore
        return digest_json(value)
    except ImportError:
        canon = json.dumps(value, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, allow_nan=False).encode("utf-8")
        return "sha256:json-c14n:v1:" + hashlib.sha256(canon).hexdigest()


def _digest_value(v: Any) -> str:
    """Digest ANY value with the same type rule arcaeon-compact freezes:
    bytes -> raw-bytes of the bytes as-is; str -> raw-bytes of UTF-8;
    anything else -> json-c14n of the canonicalized value."""
    if isinstance(v, (bytes, bytearray)):
        return _digest_bytes_raw(bytes(v))
    if isinstance(v, str):
        return _digest_bytes_raw(v.encode("utf-8"))
    return _digest_json_c14n(v)


def _well_formed(digest: Any) -> bool:
    parts = digest.split(":") if isinstance(digest, str) else []
    if len(parts) != 4 or not all(parts):
        return False
    try:
        int(parts[3], 16)
    except ValueError:
        return False
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# estimate_tokens — a cheap, documented heuristic. NOT a real tokenizer.
# ---------------------------------------------------------------------------

def estimate_tokens(data: Any) -> int:
    """Approximate token count: ~1 token per 4 characters.

    This is a heuristic, not a tokenizer call — no dependency, no model-
    specific vocabulary, no encoding table. It will be wrong, sometimes by a
    lot, on code, non-English text, and highly repetitive strings. Use it to
    size a budget cheaply, never to predict a bill.
    """
    if data is None:
        return 0
    if isinstance(data, (dict, list)):
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(data, (bytes, bytearray)):
        text = bytes(data).decode("utf-8", errors="replace")
    elif isinstance(data, str):
        text = data
    else:
        text = str(data)
    if not text:
        return 0
    return max(1, len(text) // 4)


def _char_budget(token_budget: int) -> int:
    """Convert a token budget to the char budget the strategies work in."""
    return max(1, token_budget * 4)


# ---------------------------------------------------------------------------
# DropReceipt — the honesty hook
# ---------------------------------------------------------------------------

@dataclass
class DropReceipt:
    """What `distill()` cut, provably.

    `drops` is a list of dicts, each:
      {"kind": "string_truncated" | "list_truncated" | "rows_dropped"
               | "sentences_dropped",
       "path": <str, best-effort location: a JSON-path-ish string,
                a row range, or "text">,
       "digest": <self-describing digest of the CUT content, so a holder of
                  the original can prove a specific drop belongs to it>,
       "dropped_bytes": <int>,
       "dropped_count": <int, item/row/char count as appropriate>}

    Digests are of the DROPPED content only — never the kept content and
    never the full input verbatim — so the receipt can be handed to a third
    party without also handing them the input it distilled.
    """
    schema: str
    strategy: str
    budget_tokens: int
    full: dict
    distilled: dict
    drops: list
    truncated: bool
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "strategy": self.strategy,
            "budget_tokens": self.budget_tokens,
            "full": self.full,
            "distilled": self.distilled,
            "drops": self.drops,
            "truncated": self.truncated,
            "created_at": self.created_at,
        }

    def seal(self, ledger_path, *, distiller: str = "arcaeon-distill") -> dict:
        """Chain this receipt onto an arcaeon-ledger log (optional dependency).

        Raises ImportError with a clear message if arcaeon_ledger isn't
        installed — this method is the ONLY place in the package that needs
        it; distill() itself never requires it.
        """
        try:
            from arcaeon_ledger import Ledger  # type: ignore
        except ImportError as e:
            raise ImportError(
                "DropReceipt.seal() requires arcaeon-ledger "
                "(pip install arcaeon-ledger) — distill() itself does not."
            ) from e
        row = self.to_dict()
        row["kind"] = "distill_receipt"
        row["distiller"] = distiller
        row["chain"] = Ledger(ledger_path).append(row)
        return row


def verify_receipt(receipt: "DropReceipt | dict") -> dict:
    """Self-consistency check over a DropReceipt (or its dict / sealed row).

    Checks: known schema, well-formed digests throughout, non-negative
    counts, and that `truncated` agrees with whether `drops` is non-empty.
    Does not (cannot, without the original content) re-derive the drops from
    scratch — that would require holding the full input, which the receipt
    deliberately never carries. Compare `full.digest` against a digest of
    content you hold to confirm the receipt describes YOUR input.

    Returns {"ok": bool, "notes": [<str>, ...]}.
    """
    row = receipt.to_dict() if isinstance(receipt, DropReceipt) else dict(receipt)
    notes: list = []
    if row.get("schema") != SCHEMA:
        notes.append(f"unknown schema {row.get('schema')!r} — cannot verify")
        return {"ok": False, "notes": notes}
    try:
        full, distilled, drops = row["full"], row["distilled"], row["drops"]
        truncated = row["truncated"]
    except KeyError as e:
        notes.append(f"malformed receipt: missing {e}")
        return {"ok": False, "notes": notes}

    ok = True
    for label, d in [("full.digest", full.get("digest")),
                      ("distilled.digest", distilled.get("digest"))] + \
                     [(f"drops[{i}].digest", dr.get("digest"))
                      for i, dr in enumerate(drops)]:
        if not _well_formed(d):
            ok = False
            notes.append(f"{label} is not a well-formed self-describing digest")
    for label, n in [("full.bytes", full.get("bytes")),
                      ("distilled.bytes", distilled.get("bytes"))] + \
                     [(f"drops[{i}].dropped_bytes", dr.get("dropped_bytes"))
                      for i, dr in enumerate(drops)]:
        if not (isinstance(n, int) and n >= 0):
            ok = False
            notes.append(f"{label} must be a non-negative integer")
    if bool(drops) != bool(truncated):
        ok = False
        notes.append(
            f"truncated={truncated!r} disagrees with drops "
            f"({'non-empty' if drops else 'empty'}) — receipt is inconsistent")
    if ok:
        notes.append("self-consistent")
    return {"ok": ok, "notes": notes}


def _receipt_full(full_value: Any) -> dict:
    if isinstance(full_value, (bytes, bytearray)):
        b = bytes(full_value)
    elif isinstance(full_value, str):
        b = full_value.encode("utf-8")
    else:
        b = json.dumps(full_value, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False).encode("utf-8")
    return {"digest": _digest_value(full_value), "bytes": len(b)}


# ---------------------------------------------------------------------------
# DistilledResult
# ---------------------------------------------------------------------------

@dataclass
class DistilledResult:
    content: Any
    strategy: str
    budget_tokens: int
    est_tokens_before: int
    est_tokens_after: int
    truncated: bool
    receipt: Optional[DropReceipt] = None

    def __str__(self) -> str:
        pct = (0 if self.est_tokens_before == 0 else
               100 * (self.est_tokens_before - self.est_tokens_after) // self.est_tokens_before)
        return (f"distilled via {self.strategy}: {self.est_tokens_before} -> "
                f"{self.est_tokens_after} est. tokens (~{pct}% cut, budget "
                f"{self.budget_tokens}, truncated={self.truncated})")


# ---------------------------------------------------------------------------
# JSON strategy
# ---------------------------------------------------------------------------

_DEFAULT_STR_CAP = 300
_DEFAULT_LIST_CAP = 20
_MIN_STR_CAP = 20
_MIN_LIST_CAP = 2
_MAX_SHRINK_ITERS = 12


def _walk_json(obj: Any, str_cap: int, list_cap: int, path: str, drops: list) -> Any:
    if isinstance(obj, dict):
        return {k: _walk_json(v, str_cap, list_cap, f"{path}.{k}" if path else str(k), drops)
                for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) > list_cap:
            head_n = (list_cap + 1) // 2
            tail_n = list_cap - head_n
            head = [_walk_json(it, str_cap, list_cap, f"{path}[{i}]", drops)
                    for i, it in enumerate(obj[:head_n])]
            tail_start = len(obj) - tail_n
            tail = [_walk_json(it, str_cap, list_cap, f"{path}[{tail_start + j}]", drops)
                    for j, it in enumerate(obj[tail_start:])] if tail_n else []
            dropped_slice = obj[head_n:tail_start]
            marker = f"...+{len(dropped_slice)} more items"
            drops.append({
                "kind": "list_truncated",
                "path": path or "$",
                "digest": _digest_json_c14n(dropped_slice),
                "dropped_bytes": len(json.dumps(dropped_slice, ensure_ascii=False,
                                                 separators=(",", ":")).encode("utf-8")),
                "dropped_count": len(dropped_slice),
            })
            return head + [marker] + tail
        return [_walk_json(it, str_cap, list_cap, f"{path}[{i}]", drops)
                for i, it in enumerate(obj)]
    if isinstance(obj, str):
        if len(obj) > str_cap:
            cut = obj[str_cap:]
            drops.append({
                "kind": "string_truncated",
                "path": path or "$",
                "digest": _digest_bytes_raw(cut.encode("utf-8")),
                "dropped_bytes": len(cut.encode("utf-8")),
                "dropped_count": len(cut),
            })
            return obj[:str_cap] + f"...+{len(cut)} more chars"
        return obj
    return obj


def _distill_json(parsed: Any, char_budget: int) -> tuple:
    """Returns (content, drops, truncated). Shrinks caps deterministically
    until under budget or the floor is reached; same input always walks the
    same sequence of caps."""
    str_cap, list_cap = _DEFAULT_STR_CAP, _DEFAULT_LIST_CAP
    content, drops = parsed, []
    for _ in range(_MAX_SHRINK_ITERS):
        drops = []
        content = _walk_json(parsed, str_cap, list_cap, "", drops)
        size = len(json.dumps(content, ensure_ascii=False, separators=(",", ":")))
        if size <= char_budget or (str_cap <= _MIN_STR_CAP and list_cap <= _MIN_LIST_CAP):
            break
        str_cap = max(_MIN_STR_CAP, str_cap // 2)
        list_cap = max(_MIN_LIST_CAP, list_cap // 2)
    return content, drops, bool(drops)


# ---------------------------------------------------------------------------
# Tabular strategy
# ---------------------------------------------------------------------------

_DEFAULT_ROW_CAP = 40
_MIN_ROW_CAP = 4


def _is_row_list(data: Any) -> bool:
    if not isinstance(data, list) or len(data) < 2:
        return False
    if all(isinstance(r, dict) for r in data):
        return True
    if all(isinstance(r, (list, tuple)) for r in data):
        lens = {len(r) for r in data}
        return len(lens) <= 2  # allow a header row of a different width
    return False


def _looks_tabular_text(s: str) -> bool:
    lines = [ln for ln in s.strip("\n").split("\n") if ln.strip()]
    if len(lines) < 3:
        return False
    for delim in ("\t", "|", ","):
        counts = [ln.count(delim) for ln in lines]
        if counts[0] > 0 and len(set(counts)) <= 2:
            return True
    return False


def _distill_rows(rows: list, row_cap: int) -> tuple:
    """rows: list of dict or list/tuple rows (uniform-ish). Returns
    (kept_rows, dropped_row_count, dropped_digest, dropped_bytes)."""
    if len(rows) <= row_cap:
        return rows, 0, None, 0
    head_n = (row_cap + 1) // 2
    tail_n = row_cap - head_n
    dropped = rows[head_n: len(rows) - tail_n] if tail_n else rows[head_n:]
    kept = rows[:head_n] + (rows[len(rows) - tail_n:] if tail_n else [])
    blob = json.dumps(dropped, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return kept, len(dropped), _digest_json_c14n(dropped), len(blob)


def _distill_tabular_rows(data: list, char_budget: int) -> tuple:
    row_cap = _DEFAULT_ROW_CAP
    kept, drops = data, []
    kept_out = kept  # guarantees a bound value if _MAX_SHRINK_ITERS were ever <= 0
    for _ in range(_MAX_SHRINK_ITERS):
        kept, n_dropped, digest, dbytes = _distill_rows(data, row_cap)
        drops = []
        if n_dropped:
            head_n = (row_cap + 1) // 2
            marker = {"__distilled_dropped_rows__": n_dropped} if isinstance(data[0], dict) \
                else [f"...+{n_dropped} rows dropped..."]
            kept_out = list(kept[:head_n]) + [marker] + list(kept[head_n:])
            drops.append({
                "kind": "rows_dropped", "path": "$",
                "digest": digest, "dropped_bytes": dbytes, "dropped_count": n_dropped,
            })
        else:
            kept_out = kept
        size = len(json.dumps(kept_out, ensure_ascii=False, separators=(",", ":")))
        if size <= char_budget or row_cap <= _MIN_ROW_CAP:
            return kept_out, drops, bool(drops)
        row_cap = max(_MIN_ROW_CAP, row_cap // 2)
    return kept_out, drops, bool(drops)


def _distill_tabular_text(s: str, char_budget: int) -> tuple:
    lines = s.strip("\n").split("\n")
    if not lines:
        return s, [], False
    header = lines[0]
    # markdown table: keep a separator row (e.g. "---|---") right after header
    body_start = 1
    sep_line = None
    if len(lines) > 1 and re.fullmatch(r"[\s|:-]+", lines[1]):
        sep_line = lines[1]
        body_start = 2
    body = lines[body_start:]

    row_cap = _DEFAULT_ROW_CAP
    while True:
        if len(body) <= row_cap:
            kept_body, dropped = body, []
        else:
            head_n = (row_cap + 1) // 2
            tail_n = row_cap - head_n
            dropped = body[head_n: len(body) - tail_n] if tail_n else body[head_n:]
            marker_line = f"...+{len(dropped)} rows dropped..."
            kept_body = body[:head_n] + [marker_line] + (body[len(body) - tail_n:] if tail_n else [])
        prefix = [header] + ([sep_line] if sep_line else [])
        out = "\n".join(prefix + kept_body)
        if len(out) <= char_budget or row_cap <= _MIN_ROW_CAP or not dropped:
            drops = []
            if dropped:
                blob = "\n".join(dropped).encode("utf-8")
                drops.append({
                    "kind": "rows_dropped", "path": "$",
                    "digest": _digest_bytes_raw(blob), "dropped_bytes": len(blob),
                    "dropped_count": len(dropped),
                })
            return out, drops, bool(drops)
        row_cap = max(_MIN_ROW_CAP, row_cap // 2)


# ---------------------------------------------------------------------------
# Free-text strategy — deterministic extractive selection, no LLM
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z0-9]+")


def _split_sentences(text: str) -> list:
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text.strip())]
    return [p for p in parts if p]


def _distill_text(text: str, char_budget: int, query: Optional[str]) -> tuple:
    sentences = _split_sentences(text)
    n = len(sentences)
    if n == 0:
        return text, [], False
    if len(text) <= char_budget:
        return text, [], False

    query_words = set(_WORD.findall(query.lower())) if query else set()

    def score(i: int, s: str) -> float:
        # U-shaped position weight: MAX of closeness-to-start / closeness-to-
        # end, so lead and conclusion sentences score high and the middle
        # scores low (a min() here would invert this into favoring the
        # middle — that was a bug caught by test_text_strategy_reassembles_
        # in_original_order and is deliberately left documented, not silently
        # fixed-and-forgotten).
        pos = max(1.0 / (i + 1), 1.0 / (n - i))
        overlap = len(query_words & set(_WORD.findall(s.lower()))) if query_words else 0
        return pos + 2.0 * overlap

    # deterministic ranking: score desc, tie-break by ascending index
    ranked = sorted(range(n), key=lambda i: (-score(i, sentences[i]), i))

    kept_idx = set()
    used = 0
    # marker overhead budgeted conservatively per gap; recomputed after selection
    for i in ranked:
        cost = len(sentences[i]) + 1
        if used + cost > char_budget and kept_idx:
            continue
        kept_idx.add(i)
        used += cost
        if used >= char_budget:
            break
    if not kept_idx:
        kept_idx = {ranked[0]}

    pieces = []
    dropped_runs = []
    cur_run = []
    prev = -2
    for i in range(n):
        if i in kept_idx:
            if cur_run:
                dropped_runs.append(cur_run)
                cur_run = []
            if prev != -2 and i != prev + 1:
                pieces.append("[...]")
            pieces.append(sentences[i])
            prev = i
        else:
            cur_run.append(sentences[i])
    if cur_run:
        dropped_runs.append(cur_run)

    out = " ".join(pieces)
    dropped_sentences = [s for i, s in enumerate(sentences) if i not in kept_idx]
    drops = []
    if dropped_sentences:
        blob = " ".join(dropped_sentences).encode("utf-8")
        drops.append({
            "kind": "sentences_dropped", "path": "text",
            "digest": _digest_bytes_raw(blob), "dropped_bytes": len(blob),
            "dropped_count": len(dropped_sentences),
        })
    return out, drops, bool(drops)


# ---------------------------------------------------------------------------
# distill() — the public entry point
# ---------------------------------------------------------------------------

_VALID_HINTS = {"json", "tabular", "text"}


def _detect_strategy(tool_output: Any, schema_hint: Optional[str]) -> tuple:
    """Returns (strategy_name, working_value). working_value is the parsed
    form the chosen strategy operates on (e.g. a str parsed to dict for json
    hint applied to a JSON string)."""
    if schema_hint is not None:
        if schema_hint not in _VALID_HINTS:
            raise ValueError(f"schema_hint must be one of {sorted(_VALID_HINTS)}, got {schema_hint!r}")
        if schema_hint == "json" and isinstance(tool_output, str):
            try:
                return "json", json.loads(tool_output)
            except ValueError:
                raise ValueError("schema_hint='json' but tool_output is not valid JSON text")
        return schema_hint, tool_output

    if isinstance(tool_output, (dict, list)):
        if isinstance(tool_output, list) and _is_row_list(tool_output):
            return "tabular", tool_output
        return "json", tool_output

    if isinstance(tool_output, str):
        stripped = tool_output.strip()
        if stripped[:1] in "{[":
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list) and _is_row_list(parsed):
                    return "tabular", parsed
                return "json", parsed
            except ValueError:
                pass
        if _looks_tabular_text(tool_output):
            return "tabular", tool_output
        return "text", tool_output

    # unsupported type: fall back to text on its str() form
    return "text", str(tool_output)


def distill(tool_output: Any, *, budget: int = 2000,
            schema_hint: Optional[str] = None,
            query: Optional[str] = None,
            receipt: bool = True) -> DistilledResult:
    """Deterministically compact `tool_output` under `budget` (tokens, approx).

    Args:
        tool_output: a dict/list (JSON), a str (JSON text, CSV/TSV/markdown
            table, or free text), or anything else (stringified and treated
            as text).
        budget: approximate token budget (see `estimate_tokens` — heuristic,
            not a real tokenizer). Converted internally to a char budget.
        schema_hint: force "json" | "tabular" | "text" instead of
            auto-detecting from the input's shape.
        query: optional relevance string for the text strategy's sentence
            ranking (keyword overlap). Ignored by the json/tabular strategies.
        receipt: compute a DropReceipt (cheap stdlib hashing). Set False to
            skip the extra digesting on a hot path that doesn't need it.

    Returns a DistilledResult. Same (tool_output, budget, schema_hint, query)
    always returns the same `.content`, byte-for-byte — that determinism is
    the product; see the module docstring for why.
    """
    if budget <= 0:
        raise ValueError("budget must be a positive integer (tokens)")

    strategy, working = _detect_strategy(tool_output, schema_hint)
    char_budget = _char_budget(budget)

    if strategy == "json":
        content, drops, truncated = _distill_json(working, char_budget)
    elif strategy == "tabular":
        if isinstance(working, str):
            content, drops, truncated = _distill_tabular_text(working, char_budget)
        else:
            content, drops, truncated = _distill_tabular_rows(list(working), char_budget)
    elif strategy == "text":
        content, drops, truncated = _distill_text(str(working), char_budget, query)
    else:  # pragma: no cover — _detect_strategy only returns known names
        raise AssertionError(f"unreachable strategy {strategy!r}")

    est_before = estimate_tokens(tool_output)
    est_after = estimate_tokens(content)

    rcpt = None
    if receipt:
        rcpt = DropReceipt(
            schema=SCHEMA,
            strategy=strategy,
            budget_tokens=budget,
            full=_receipt_full(tool_output),
            distilled=_receipt_full(content),
            drops=drops,
            truncated=truncated,
        )

    return DistilledResult(
        content=content,
        strategy=strategy,
        budget_tokens=budget,
        est_tokens_before=est_before,
        est_tokens_after=est_after,
        truncated=truncated,
        receipt=rcpt,
    )
