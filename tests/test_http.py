import json

from fastapi.testclient import TestClient

from xccode.xcroute.auth import TokenAuth, digest
from xccode.xcroute.budget import BudgetGate, Limits
from xccode.xcroute.decide import PoolScore
from xccode.xcroute.events import EventLog
from xccode.xcroute.http import create_app
from xccode.xcroute.router import Completion, PoolConfig, Router

POOLS = ("coding-strong", "coding-fast", "reasoning", "fast")


def _router(tmp_path, token="secret", provider=None):
    pools = {p: PoolConfig(provider="mock", est_cost_micro_usd=100) for p in POOLS}
    scores = {p: PoolScore(quality=0.9, cost_norm=0.5, latency_norm=0.5) for p in POOLS}

    def default_provider(pool, messages):
        return Completion(text=f"hi from {pool}", tokens_in=1, tokens_out=2, cost_micro_usd=100)

    return Router(
        auth=TokenAuth({"opencode": digest(token)}),
        pools=pools,
        scores=scores,
        healthy=lambda: set(POOLS),
        budget=BudgetGate(Limits(per_request=1_000_000, per_day=100_000_000)),
        provider=provider or default_provider,
        events=EventLog(tmp_path / "events.db"),
        pending_dir=tmp_path / "learn/pending",
    )


def _client(tmp_path, token="secret"):
    return TestClient(create_app(_router(tmp_path, token)))


def _chat(client, model="xc/auto", token="secret", content="fix the bug"):
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": content}]},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_healthz(tmp_path):
    assert _client(tmp_path).get("/healthz").json() == {"ok": True}


def test_chat_requires_token(tmp_path):
    r = _client(tmp_path).post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


def test_chat_completion_ok(tmp_path):
    r = _chat(_client(tmp_path))
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"].startswith("hi from ")
    assert body["usage"]["total_tokens"] == 3


def test_unknown_model_is_400(tmp_path):
    r = _chat(_client(tmp_path), model="does-not-exist")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "400"


def test_wrong_token_is_401(tmp_path):
    r = _chat(_client(tmp_path), token="wrong")
    assert r.status_code == 401


def test_events_door_returns_routing_log(tmp_path):
    client = _client(tmp_path)
    assert _chat(client).status_code == 200
    r = client.get("/events", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    events = r.json()["events"]
    assert len(events) == 1
    assert events[0]["agent"] == "opencode"


def test_ctx_door_requires_token(tmp_path):
    client = _client(tmp_path)
    assert client.get("/ctx").status_code == 401
    assert client.get("/ctx", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_learn_door_queues_a_valid_digest(tmp_path):
    from xccode.xcroute.learn import digest_id

    content = {
        "source": "thoughts",
        "repo": "xccode",
        "task": "fix the thing",
        "files_touched": ["src/a.py"],
        "commands": ["pytest"],
        "outcome": "tests pass",
        "notes": "reviewed",
    }
    digest = {"id": digest_id(content), "collected_at": "2026-10-04T00:00:00Z", **content}
    client = _client(tmp_path)
    r = client.post("/learn", json=digest, headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    assert r.json()["queued"] == 1
    assert (tmp_path / "learn/pending" / f"{digest['id']}.json").exists()


def test_learn_door_rejects_a_malformed_digest(tmp_path):
    client = _client(tmp_path)
    r = client.post("/learn", json={"task": "no id"}, headers={"Authorization": "Bearer secret"})
    assert r.status_code == 400
    assert r.json()["queued"] == 0


def test_learn_door_requires_token(tmp_path):
    client = _client(tmp_path)
    assert client.post("/learn", json={"task": "x"}).status_code == 401


def test_ctx_door_reads_memory(tmp_path):
    from xccode.xcroute.memory import OpenVikingMemory

    class FakeMemory(OpenVikingMemory):
        def read(self, uri, tier="L1"):
            return "hello memory"

        def search(self, query, max_chars=1500):
            return [{"uri": "viking://x", "content": "found"}]

        def brief(self, repo):
            return [{"uri": "viking://projects/x", "content": "brief"}]

    router = _router(tmp_path)
    router.memory = FakeMemory(base_url="http://ov.test")
    client = TestClient(create_app(router))
    r = client.get(
        "/ctx",
        params={"op": "read", "uri": "viking://x", "tier": "L2"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 200
    assert r.json()["items"][0]["content"] == "hello memory"
    r2 = client.get(
        "/ctx", params={"op": "search", "q": "x"}, headers={"Authorization": "Bearer secret"}
    )
    assert r2.json()["items"][0]["content"] == "found"
    r3 = client.get(
        "/ctx", params={"op": "brief", "repo": "xccode"}, headers={"Authorization": "Bearer secret"}
    )
    assert r3.json()["items"][0]["content"] == "brief"


def test_ctx_door_without_memory_returns_empty(tmp_path):
    client = _client(tmp_path)
    r = client.get(
        "/ctx",
        params={"op": "read", "uri": "viking://x"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_chat_completion_streaming_sse(tmp_path):
    client = _client(tmp_path)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "xc/auto",
            "stream": True,
            "messages": [{"role": "user", "content": "fix the bug"}],
        },
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert "data: " in body
    assert "data: [DONE]" in body
    assert '"finish_reason": "stop"' in body
    assert "hi from coding-strong" in body  # "fix the bug" -> coding floor


def test_chat_stream_frames_are_json_escaped(tmp_path):
    # a provider reply containing SSE delimiters must not split the stream or fake a [DONE]
    text = "a\n\ndata: [DONE]\n\nb"

    def provider(pool, messages):
        return Completion(text=text, tokens_in=1, tokens_out=2, cost_micro_usd=1)

    client = TestClient(create_app(_router(tmp_path, provider=provider)))
    r = client.post(
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "fix the bug"}]},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 200
    frames = [f for f in r.text.split("\n\n") if f]
    assert frames[-1] == "data: [DONE]"  # the terminator only
    assert len(frames) == 3  # content delta, finish, [DONE]
    delta = json.loads(frames[0][len("data: "):])
    assert delta["choices"][0]["delta"]["content"] == text  # reply survives round-trip intact


def test_chat_stream_provider_error_is_json(tmp_path):
    from xccode.xcroute.router import ProviderError

    def provider(pool, messages):
        raise ProviderError("boom")

    client = TestClient(create_app(_router(tmp_path, provider=provider)))
    r = client.post(
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "fix the bug"}]},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "502"


TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "write a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}


def test_tools_are_forwarded_and_tool_calls_returned(tmp_path):
    """§7: a dsh job sends OpenAI tool definitions; the provider must see them and its
    tool_calls answer must reach the caller with finish_reason=tool_calls."""
    seen: dict = {}

    def provider(pool, messages, tools=None, tool_choice=None):
        seen["tools"] = tools
        seen["tool_choice"] = tool_choice
        return Completion(
            text="",
            tokens_in=1,
            tokens_out=2,
            cost_micro_usd=100,
            tool_calls=({"id": "call_1", "type": "function",
                         "function": {"name": "write_file", "arguments": "{}"}},),
        )

    client = TestClient(create_app(_router(tmp_path, provider=provider)))
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "xc/auto",
            "messages": [{"role": "user", "content": "create a file"}],
            "tools": [TOOL],
            "tool_choice": "auto",
        },
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 200
    assert seen["tools"] == [TOOL] and seen["tool_choice"] == "auto"
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "write_file"


def test_tool_results_round_trip_in_messages(tmp_path):
    """The tool loop's second turn carries assistant tool_calls + a tool result; both must reach
    the provider (and Guard-lite must tolerate content-less turns)."""
    captured: list = []

    def provider(pool, messages, tools=None, tool_choice=None):
        captured.extend(messages)
        return Completion(text="done", tokens_in=1, tokens_out=2, cost_micro_usd=100)

    client = TestClient(create_app(_router(tmp_path, provider=provider)))
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "xc/auto",
            "messages": [
                {"role": "user", "content": "create a file"},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "call_1", "type": "function",
                                 "function": {"name": "write_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "name": "write_file",
                 "content": "ok"},
            ],
            "tools": [TOOL],
        },
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 200
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call_1"
               for m in captured)


def test_streaming_forwards_tool_calls_as_deltas(tmp_path):
    """OpenCode/dsh stream (SSE): a tool_calls reply must emerge as tool_call deltas and
    finish_reason=tool_calls, not a bare content+stop (§7)."""
    def provider(pool, messages, tools=None, tool_choice=None):
        return Completion(
            text="", tokens_in=1, tokens_out=2, cost_micro_usd=100,
            tool_calls=({"id": "call_9", "type": "function",
                         "function": {"name": "bash_exec", "arguments": "{\"cmd\":\"ls\"}"}},),
        )

    client = TestClient(create_app(_router(tmp_path, provider=provider)))
    with client.stream(
        "POST", "/v1/chat/completions",
        json={"model": "xc/auto",
              "messages": [{"role": "user", "content": "ls"}],
              "tools": [TOOL], "stream": True},
        headers={"Authorization": "Bearer secret"},
    ) as r:
        frames = [ln[6:] for ln in r.iter_lines() if ln.startswith("data: {")]
    parsed = [json.loads(f) for f in frames]
    deltas = [p["choices"][0]["delta"] for p in parsed]
    finishes = [p["choices"][0]["finish_reason"] for p in parsed]
    assert any(d.get("tool_calls") for d in deltas)
    assert finishes[-1] == "tool_calls"
    call = next(d["tool_calls"][0] for d in deltas if d.get("tool_calls"))
    assert call["function"]["name"] == "bash_exec"
