"""Tests for xccode-mcp, the stdio MCP server (XC-CODE-001 §4.11)."""

from xccode import xcmcp


def _call(method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return xcmcp.handle(msg)


def test_initialize_capabilities():
    r = _call("initialize", {})
    assert r["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in r["result"]["capabilities"]
    assert r["result"]["serverInfo"]["name"] == "xccode-mcp"


def test_ping():
    assert _call("ping", {})["result"] == {}


def test_tools_list_exposes_all_six():
    names = {t["name"] for t in _call("tools/list", {})["result"]["tools"]}
    assert names == {
        "ctx_brief",
        "ctx_search",
        "ctx_read",
        "job_start",
        "job_status",
        "job_collect",
    }


def test_notification_has_no_response():
    assert xcmcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tool_call_job_status_is_honest_stub():
    r = _call("tools/call", {"name": "job_status", "arguments": {"job_id": "abc"}})
    assert r["result"]["isError"] is False
    assert "dsh is not wired" in r["result"]["content"][0]["text"]


def test_tool_call_unknown_is_error():
    r = _call("tools/call", {"name": "nope", "arguments": {}})
    assert r["error"]["code"] == -32602


def test_tool_call_bad_args_is_error_result():
    r = _call("tools/call", {"name": "ctx_brief", "arguments": {}})
    assert r["result"]["isError"] is True


def test_unknown_method_is_error():
    r = _call("frobnicate", {})
    assert r["error"]["code"] == -32601
