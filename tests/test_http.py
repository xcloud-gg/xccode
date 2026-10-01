from fastapi.testclient import TestClient

from xccode.xcroute.auth import TokenAuth, digest
from xccode.xcroute.budget import BudgetGate, Limits
from xccode.xcroute.decide import PoolScore
from xccode.xcroute.events import EventLog
from xccode.xcroute.http import create_app
from xccode.xcroute.router import Completion, PoolConfig, Router

POOLS = ("coding-strong", "coding-fast", "reasoning", "fast")


def _router(tmp_path, token="secret"):
    pools = {p: PoolConfig(provider="mock", est_cost_micro_usd=100) for p in POOLS}
    scores = {p: PoolScore(quality=0.9, cost_norm=0.5, latency_norm=0.5) for p in POOLS}

    def provider(pool, messages):
        return Completion(text=f"hi from {pool}", tokens_in=1, tokens_out=2, cost_micro_usd=100)

    return Router(
        auth=TokenAuth({"opencode": digest(token)}),
        pools=pools,
        scores=scores,
        healthy=lambda: set(POOLS),
        budget=BudgetGate(Limits(per_request=1_000_000, per_day=100_000_000)),
        provider=provider,
        events=EventLog(tmp_path / "events.db"),
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
