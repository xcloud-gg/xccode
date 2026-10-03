"""FastAPI HTTP layer for xcroute (XC-CODE-001 §4.4, §4.5).

Wraps the offline `Router` with an OpenAI-compatible `/v1/chat/completions` endpoint and the
memory/learning/events doors. Authentication is `Authorization: Bearer <token>`; the router maps
the token to an agent name (only digests are stored). The router keeps no network code — the
provider stays injected — so the app is testable against a mock provider.
"""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI
from fastapi import Request as HTTPRequest
from fastapi.responses import JSONResponse
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
        return _respond(body.model, r)

    @app.get("/ctx")
    def ctx(http: HTTPRequest):
        agent = router.auth.agent_for(_bearer(http.headers.get("authorization")))
        if agent is None:
            return _unauthorized()
        # read-only memory door; wired to OpenViking in a later increment
        return {"agent": agent, "items": []}

    @app.post("/learn")
    def learn(http: HTTPRequest):
        agent = router.auth.agent_for(_bearer(http.headers.get("authorization")))
        if agent is None:
            return _unauthorized()
        # learning intake; wired to the Hermes learner in a later increment
        return {"queued": 0}

    @app.get("/events")
    def events(http: HTTPRequest):
        agent = router.auth.agent_for(_bearer(http.headers.get("authorization")))
        if agent is None:
            return _unauthorized()
        return {"events": router.events.all()}

    return app
