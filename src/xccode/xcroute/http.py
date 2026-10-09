"""FastAPI HTTP layer for xcroute (XC-CODE-001 §4.4, §4.5).

Wraps the offline `Router` with an OpenAI-compatible `/v1/chat/completions` endpoint and the
memory/learning/events doors. Authentication is `Authorization: Bearer <token>`; the router maps
the token to an agent name (only digests are stored). The router keeps no network code — the
provider stays injected — so the app is testable against a mock provider.
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import FastAPI
from fastapi import Request as HTTPRequest
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .router import AUTO, Request, Result, Router

_STATUS: dict[int, int] = {400: 400, 401: 401, 403: 403, 429: 429, 502: 502, 503: 503}

# Which agents may open each door (advisor O6). ``/v1/chat/completions`` is open to every
# authenticated agent; the side doors are not — in particular `dsh-job` (a semi-autonomous
# background agent) may neither learn into memory (poisoning) nor read memory (`/ctx`), and may
# only report telemetry to `/events` alongside hermes and dsh-bench per §4.4.
DOOR_AGENTS: dict[str, set[str]] = {
    "/ctx": {"opencode", "hermes", "openviking"},
    "/learn": {"opencode"},
    "/events": {"hermes", "dsh-bench", "dsh-job"},
}


def _forbidden_door(agent: str, door: str) -> JSONResponse | None:
    if agent not in DOOR_AGENTS[door]:
        return JSONResponse(
            status_code=403, content={"error": {"message": f"not allowed: {door}", "type": "auth"}}
        )
    return None


class Message(BaseModel):
    role: str
    content: str | None = None
    # Tool-call loop fields (§7): assistant turns may carry tool_calls; tool results carry
    # tool_call_id/name. Content is None on pure tool-call turns.
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatRequest(BaseModel):
    model: str = AUTO
    messages: list[Message] = Field(default_factory=list)
    session: str | None = None
    stream: bool = False
    # OpenAI-style tool definitions, passed through to the provider untouched (dsh jobs, §7).
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None


def _bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:]
    return None


def _completion(model: str, r: Result) -> dict:
    ev = r.event
    ti = ev.tokens_in if ev else 0
    to = ev.tokens_out if ev else 0
    message: dict = {"role": "assistant", "content": r.text}
    if r.tool_calls:
        message["tool_calls"] = [dict(tc) for tc in r.tool_calls]
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if r.tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": ti, "completion_tokens": to, "total_tokens": ti + to},
    }


def _respond(model: str, r: Result):
    if r.status == 200:
        return _completion(model, r)
    return JSONResponse(
        status_code=_STATUS.get(r.status, 500),
        content={"error": {"message": r.detail, "type": "xccode", "code": str(r.status)}},
    )


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": {"message": "bad token"}})


def _stream_chunks(model: str, r: Result):
    """Yield OpenAI-compatible SSE chunks for a completed Result (XC-CODE-001 §4.4).

    xcroute calls the provider non-streaming (OmniRoute's own streaming is not plumbed through yet),
    so the whole completion is emitted as one delta chunk, then a finish chunk, then [DONE]. Clients
    that send ``stream: true`` (OpenCode and most OpenAI-compatible tools) require this shape.
    """
    base = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }

    def chunk(delta: dict, finish_reason: str | None) -> str:
        return "data: " + json.dumps(
            {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
        ) + "\n\n"

    yield chunk({"role": "assistant", "content": r.text or None}, None)
    if r.tool_calls:
        # One delta per tool call carries the full call, indexed by position (SSE joins deltas by
        # index; parallel calls must keep distinct indexes or clients merge them into one — C6),
        # then finish_reason=tool_calls. The call completes internally first, so there is nothing
        # to stream incrementally — only the finished calls to forward (§7).
        for i, tc in enumerate(r.tool_calls):
            yield chunk({"tool_calls": [{**tc, "index": i}]}, None)
        yield chunk({}, "tool_calls")
    else:
        yield chunk({}, "stop")
    yield "data: [DONE]\n\n"


def create_app(router: Router) -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/v1/chat/completions")
    def chat_completions(body: ChatRequest, http: HTTPRequest):
        token = _bearer(http.headers.get("authorization"))
        session = body.session or http.headers.get("x-xccode-session") or "default"
        r = router.handle(
            Request(
                token=token,
                model=body.model,
                messages=[m.model_dump(exclude_none=True) for m in body.messages],
                session=session,
                tools=body.tools,
                tool_choice=body.tool_choice,
            )
        )
        if body.stream and r.status == 200:
            return StreamingResponse(
                _stream_chunks(body.model, r),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return _respond(body.model, r)

    @app.get("/ctx")
    def ctx(
        http: HTTPRequest,
        op: str = "read",
        uri: str | None = None,
        tier: str = "L1",
        q: str | None = None,
        repo: str | None = None,
    ):
        agent = router.auth.agent_for(_bearer(http.headers.get("authorization")))
        if agent is None:
            return _unauthorized()
        denied = _forbidden_door(agent, "/ctx")
        if denied is not None:
            return denied
        if router.memory is None:
            return {"agent": agent, "items": []}
        if op == "search" and q:
            return {"agent": agent, "items": router.memory.search(q)}
        if op == "brief" and repo:
            return {"agent": agent, "items": router.memory.brief(repo)}
        if op == "read" and uri:
            content = router.memory.read(uri, tier)
            return {"agent": agent, "items": [{"uri": uri, "tier": tier, "content": content}]}
        return {"agent": agent, "items": []}

    @app.post("/learn")
    def learn(body: dict, http: HTTPRequest):
        agent = router.auth.agent_for(_bearer(http.headers.get("authorization")))
        if agent is None:
            return _unauthorized()
        denied = _forbidden_door(agent, "/learn")
        if denied is not None:
            return denied
        queued, problems = router.learn(body)
        if problems:
            return JSONResponse(
                status_code=400,
                content={"queued": 0, "agent": agent, "rejected": problems},
            )
        return {"queued": 1, "agent": agent, "id": body.get("id")}

    @app.get("/events")
    def events(http: HTTPRequest):
        agent = router.auth.agent_for(_bearer(http.headers.get("authorization")))
        if agent is None:
            return _unauthorized()
        denied = _forbidden_door(agent, "/events")
        if denied is not None:
            return denied
        return {"events": router.events.all()}

    return app
