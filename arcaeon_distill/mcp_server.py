"""distill MCP server — drop `distill()` into any MCP agent with no code.

Zero dependencies: MCP is JSON-RPC 2.0 over stdio, so this speaks it directly
rather than pulling the SDK (keeps the whole product install-free). Any MCP
client (Claude Code, etc.) gets one tool:

  distill_tool_output(tool_output, budget, schema_hint, query, receipt)
      -> {content, strategy, est_tokens_before, est_tokens_after,
          truncated, receipt}

Run:  python -m arcaeon_distill.mcp_server
Wire into an MCP client (e.g. Claude Code .mcp.json):
  { "mcpServers": { "distill": {
      "command": "python", "args": ["-m", "arcaeon_distill.mcp_server"] } } }

Implements the slice of MCP a tool server needs: initialize, tools/list,
tools/call. Protocol version 2025-06-18. Notifications are ignored (no id).
This module is import-guarded from the rest of the package: `arcaeon_distill`
itself never imports this file, so `distill()` works with zero MCP awareness
and this server is only touched if you run it directly.
"""
from __future__ import annotations

import json
import sys

from arcaeon_distill import distill, __version__

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "distill_tool_output",
        "description": (
            "Deterministically compact a large tool output (JSON, a table, or "
            "free text) under a token budget. Same input at the same budget "
            "always returns byte-identical output (cache-stable — see the "
            "package docstring for why that matters under prompt caching). "
            "Does NOT guarantee lower billed cost, only a smaller, more "
            "reliable context footprint. Returns a drop receipt describing "
            "exactly what was cut, so you can re-fetch the original if the "
            "cut looks load-bearing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_output": {
                    "description": "The large tool output to distill: a JSON "
                                   "object/array, or a string (JSON text, a "
                                   "CSV/TSV/markdown table, or free text).",
                },
                "budget": {
                    "type": "integer", "minimum": 1, "default": 2000,
                    "description": "Approximate token budget (heuristic, ~chars/4).",
                },
                "schema_hint": {
                    "type": "string", "enum": ["json", "tabular", "text"],
                    "description": "Force a strategy instead of auto-detecting.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional relevance query for the text "
                                   "strategy's sentence ranking.",
                },
                "timestamp": {
                    "type": "boolean",
                    "description": "Include the receipt's wall-clock "
                                   "created_at. Off by default: it would make "
                                   "two identical calls return different "
                                   "bytes and bust your prefix cache.",
                },
                "receipt": {
                    "type": "boolean", "default": True,
                    "description": "Include a drop receipt in the response.",
                },
            },
            "required": ["tool_output"],
        },
    },
]


def _result(id_, payload):
    return {"jsonrpc": "2.0", "id": id_, "result": payload}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _text_content(obj) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, default=str)}]}


def _call_distill(args: dict) -> dict:
    if "tool_output" not in args:
        return {"error": "tool_output is required"}
    kwargs = {}
    if "budget" in args:
        kwargs["budget"] = int(args["budget"])
    if args.get("schema_hint") is not None:
        kwargs["schema_hint"] = args["schema_hint"]
    if args.get("query") is not None:
        kwargs["query"] = args["query"]
    if "receipt" in args:
        kwargs["receipt"] = bool(args["receipt"])
    result = distill(args["tool_output"], **kwargs)
    out = {
        "content": result.content,
        "strategy": result.strategy,
        "budget_tokens": result.budget_tokens,
        "est_tokens_before": result.est_tokens_before,
        "est_tokens_after": result.est_tokens_after,
        "truncated": result.truncated,
        # The receipt's `created_at` is wall-clock. Serialized into the tool
        # result it lands in the agent's context and makes the response NOT
        # byte-identical between two identical calls — busting the prefix
        # cache this tool exists to protect, on the one surface where the
        # claim is advertised. The library's own tests strip it before
        # comparing; the shipped surface has to strip it too. Ask for
        # `timestamp: true` if you want the stamp.
        "receipt": _receipt_payload(result.receipt,
                                    stamp=bool(args.get("timestamp"))),
    }
    return out


def _receipt_payload(receipt, *, stamp: bool):
    if receipt is None:
        return None
    row = receipt.to_dict()
    if not stamp:
        row.pop("created_at", None)
    return row


def handle(msg: dict):
    """Return a response dict, or None for notifications (no id)."""
    mid = msg.get("id")
    method = msg.get("method")
    if mid is None:
        return None

    if method == "initialize":
        return _result(mid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "distill", "version": __version__},
        })
    if method == "tools/list":
        return _result(mid, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "distill_tool_output":
                out = _call_distill(args)
                is_error = "error" in out and len(out) == 1
                payload = _text_content(out)
                if is_error:
                    payload["isError"] = True
                return _result(mid, payload)
            return _result(mid, {**_text_content(
                {"error": f"unknown tool {name}"}), "isError": True})
        except Exception as e:  # never crash the server on one bad call
            return _result(mid, {**_text_content({"error": str(e)}), "isError": True})

    return _error(mid, -32601, f"method not found: {method}")


def main(argv=None) -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # not JSON-RPC; skip
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, default=str) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
