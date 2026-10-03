"""xccode-mcp — OpenCode's door into memory and jobs (XC-CODE-001 §4.11).

A small Model Context Protocol server over stdio (newline-delimited JSON-RPC 2.0) that exposes
``ctx_brief`` / ``ctx_search`` / ``ctx_read`` (read-only memory, via xcroute's ``/ctx`` door) and
``job_start`` / ``job_status`` / ``job_collect`` (background dsh jobs, via ``systemd-run --user``).

Skills are deliberately not served here: approved skills install as native OpenCode skills.

The protocol surface is the small, stable core — ``initialize``, ``ping``, ``tools/list``,
``tools/call`` — so no third-party MCP SDK is needed.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

XCROUTE = os.environ.get("XCC_ROUTE", "http://127.0.0.1:18080")
PROTOCOL_VERSION = "2024-11-05"


def _token() -> str:
    return os.environ.get("XCC_TOKEN", "")


def _ctx(path: str = "/ctx") -> dict:
    r = httpx.get(
        f"{XCROUTE}{path}",
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def _ctx_items(kind: str, ref: str) -> str:
    try:
        data = _ctx("/ctx")
    except httpx.HTTPStatusError as e:
        return f"({kind}) xcroute /ctx returned {e.response.status_code}: {e.response.text}"
    items = data.get("items", [])
    if not items:
        return f"({kind}) no memory yet for {ref!r} — OpenViking is wired in a later increment (M5)"
    return "\n".join(json.dumps(i, sort_keys=True) for i in items)


def ctx_brief(repo: str) -> str:
    """Repository abstract, overview, dated rules and pitfalls (at most ~1500 tokens)."""
    return _ctx_items("ctx_brief", repo)


def ctx_search(query: str) -> str:
    """Search the repository's memory for ``query``."""
    return _ctx_items("ctx_search", query)


def ctx_read(path: str, tier: str = "L1") -> str:
    """Read a memory entry by path and tier (L0 abstract / L1 overview / L2 full)."""
    return _ctx_items("ctx_read", path)


def _job(job_id: str) -> str:
    return f"job {job_id!r}: dsh is not wired yet (M5)"


def job_start(repo: str, task: str) -> str:
    """Start an isolated background job (a dsh worktree session) for a long/parallel task."""
    return f"job_start: dsh is not wired yet (M5); would run {task!r} in {repo!r}"


def job_status(job_id: str) -> str:
    """Report the state of a background job started with job_start."""
    return _job(job_id)


def job_collect(job_id: str) -> str:
    """Collect the summary, diff stat and test result of a finished background job."""
    return _job(job_id)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "ctx_brief",
        "description": "Load the repository's abstract, overview, dated rules and pitfalls.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string", "description": "repository path or name"}},
            "required": ["repo"],
        },
    },
    {
        "name": "ctx_search",
        "description": "Search the repository's memory for a query.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "search query"}},
            "required": ["query"],
        },
    },
    {
        "name": "ctx_read",
        "description": "Read a memory entry by path and loading tier.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "viking:// path"},
                "tier": {
                    "type": "string",
                    "enum": ["L0", "L1", "L2"],
                    "description": "L0 abstract / L1 overview / L2 full",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "job_start",
        "description": "Start an isolated background job for a long or parallel task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "task": {"type": "string", "description": "the task to run"},
            },
            "required": ["repo", "task"],
        },
    },
    {
        "name": "job_status",
        "description": "Report the state of a background job.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "job_collect",
        "description": "Collect the result of a finished background job.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
]

_HANDLERS = {
    "ctx_brief": ctx_brief,
    "ctx_search": ctx_search,
    "ctx_read": ctx_read,
    "job_start": job_start,
    "job_status": job_status,
    "job_collect": job_collect,
}


def _respond(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_ok(msg_id: Any, text: str) -> dict[str, Any]:
    return _respond(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})


def _tool_error(msg_id: Any, text: str) -> dict[str, Any]:
    return _respond(msg_id, {"content": [{"type": "text", "text": text}], "isError": True})


def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _respond(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "xccode-mcp", "version": "1.0.0"},
            },
        )
    if method == "ping":
        return _respond(msg_id, {})
    if method == "tools/list":
        return _respond(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        handler = _HANDLERS.get(name)
        if handler is None:
            return _error(msg_id, -32602, f"unknown tool: {name}")
        try:
            text = handler(**params.get("arguments", {}))
        except TypeError as e:  # bad arguments
            return _tool_error(msg_id, str(e))
        except Exception as e:  # noqa: BLE001 — surfaced to the caller as an error result
            return _tool_error(msg_id, f"{type(e).__name__}: {e}")
        return _tool_ok(msg_id, text)
    if msg_id is None:
        return None  # notification (e.g. notifications/initialized) — no response
    return _error(msg_id, -32601, f"method not found: {method}")


def run_stdio() -> None:
    """Serve MCP over stdio: read a JSON-RPC line, write one back."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio()
