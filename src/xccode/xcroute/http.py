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


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = AUTO
    messages: list[Message] = Field(default_factory=list)
    session: str | None = None
    stream: bool = False


def _bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:]
    return None


def _completion(model: str, r: Result) -> dict:
    ev = r.event
    ti = ev.tokens_in if ev else 0
    to = ev.tokens_out if ev else 0
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": r.text},
                "finish_reason": "stop",
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

    yield chunk({"role": "assistant", "content": r.text}, None)
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
                messages=[m.model_dump() for m in body.messages],
                session=session,
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
    def ctx(http: HTTPRequest):
        agent = router.auth.agent_for(_bearer(http.headers.get("authorization")))
        if agent is None:
            return _unauthorized()
        # read-only memory door; wired to OpenViking in a later increment
        return {"agent": agent, "items": []}

    @app.post("/learn")
    def learn(body: dict, http: HTTPRequest):
        agent = router.auth.agent_for(_bearer(http.headers.get("authorization")))
        if agent is None:
            return _unauthorized()
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
        return {"events": router.events.all()}

    return app
