"""The request path (§6.5): auth → repo marker → turn → Guard-lite → budget → decide → floor →
score → provider → routing event. Providers and Jev are injected; nothing here touches the network.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .auth import TokenAuth
from .budget import BudgetGate
from .decide import (
    Answers,
    PoolScore,
    TurnTracker,
    message_hash,
    mode_from,
    pick_pool,
    rules_answers,
)
from .events import EventLog, RoutingEvent
from .guard import redact_messages
from .markers import RepoPolicy, repo_policy

JEV_INPUT_MAX = 2048  # bytes of redacted user message sent to the decider (§6.4.5)
JEV_TIMEOUT_S = 0.8
AUTO = "xc/auto"


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class Completion:
    text: str
    tokens_in: int
    tokens_out: int
    cost_micro_usd: int
    failover: bool = False


Provider = Callable[[str, list[dict]], Completion]  # (pool, messages) -> Completion
Jev = Callable[[dict, float], Answers]  # (input, timeout_s) -> answers; raises on any failure


@dataclass(frozen=True)
class Request:
    token: str | None
    model: str
    messages: list[dict]
    session: str
    cwd: str | None = None
    remote_url: str | None = None


@dataclass
class Result:
    status: int
    pool: str | None = None
    text: str = ""
    event: RoutingEvent | None = None
    detail: str = ""


@dataclass
class PoolConfig:
    provider: str
    est_cost_micro_usd: int


@dataclass
class Router:
    auth: TokenAuth
    pools: dict[str, PoolConfig]
    scores: dict[str, PoolScore]
    healthy: Callable[[], set[str]]
    budget: BudgetGate
    provider: Provider
    events: EventLog
    jev: Jev | None = None
    role_by_prompt_hash: dict[str, str] = field(default_factory=dict)
    repo_overrides: dict[str, RepoPolicy] = field(default_factory=dict)
    lam: float = 0.2
    mu: float = 0.1
    tracker: TurnTracker = field(default_factory=TurnTracker)

    def handle(self, req: Request, now: float | None = None) -> Result:
        now = time.time() if now is None else now
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        day = ts[:10]

        agent = self.auth.agent_for(req.token)
        if agent is None:  # unauthenticated callers leave no event: nothing to attribute it to
            return Result(401, detail="bad token")

        user_text = next((m["content"] for m in reversed(req.messages)
                          if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
        sys_text = next((m["content"] for m in req.messages
                         if m.get("role") == "system" and isinstance(m.get("content"), str)), "")
        role = "unknown"
        if sys_text:
            role = self.role_by_prompt_hash.get(message_hash(sys_text), "unknown")
        mh = message_hash(user_text)

        def done(status: int, decider: str, outcome: str, mode=None, pool=None, hits=(),
                 comp: Completion | None = None, ms: int = 0, shadow=None, text="",
                 detail="") -> Result:
            ev = RoutingEvent(
                ts=ts, agent=agent, xc_agent_role=role, decider=decider, mode=mode, pool=pool,
                message_hash=mh, guard_hits=tuple(hits), outcome=outcome, shadow=shadow or {},
                tokens_in=comp.tokens_in if comp else 0, tokens_out=comp.tokens_out if comp else 0,
                cost_micro_usd=comp.cost_micro_usd if comp else 0,
                failover=comp.failover if comp else False, latency_ms=ms)
            self.events.write(ev)
            return Result(status, pool, text, ev, detail)

        policy = repo_policy(req.cwd, req.remote_url, self.repo_overrides)
        if policy is RepoPolicy.SECRET:
            return done(403, "refused", "refused:secret-repo", detail="repository marked secret")

        messages, hits = redact_messages(req.messages)
        redacted_user = next((m["content"] for m in reversed(messages)
                              if m.get("role") == "user" and isinstance(m.get("content"), str)), "")

        shadow: dict[str, str] = {}
        answers = rules_answers(redacted_user)
        mode: str | None = None

        if req.model != AUTO:  # the operator's own pinned choice: passed through, floor not applied
            if req.model not in self.pools:
                return done(400, "refused", "refused:unknown-model", hits=hits, detail=req.model)
            pool, decider = req.model, "pinned"
        elif (sticky := self.tracker.sticky(req.session, user_text)) is not None \
                and sticky in self.healthy():
            pool, decider = sticky, "sticky"
        else:
            decider = "rules"
            if policy is RepoPolicy.NORMAL and self.jev is not None:
                try:
                    jev_answers = self.jev(
                        {"message": redacted_user.encode()[:JEV_INPUT_MAX].decode(errors="ignore"),
                         "role": role}, JEV_TIMEOUT_S)
                    shadow["rules"] = mode_from(answers)
                    if not jev_answers.unsure():
                        answers, decider = jev_answers, "jev"
                except Exception:  # noqa: BLE001 - slow, down, or malformed: the rules decide
                    pass
            mode = mode_from(answers)
            pool, why = pick_pool(
                mode, answers, role, self.healthy(), self.scores, self.lam, self.mu)
            if pool is None:
                return done(503, decider, f"refused:{why}", mode=mode, hits=hits, shadow=shadow,
                            detail=why)

        cfg = self.pools[pool]
        if not self.budget.allows(day, agent, cfg.provider, cfg.est_cost_micro_usd):
            return done(429, decider, "refused:budget", mode=mode, pool=pool, hits=hits,
                        shadow=shadow, detail="budget")

        start = time.monotonic()
        try:
            comp = self.provider(pool, messages)
        except ProviderError as e:
            ms = int((time.monotonic() - start) * 1000)
            return done(502, decider, "error:provider", mode=mode, pool=pool, hits=hits,
                        shadow=shadow, ms=ms, detail=str(e))
        ms = int((time.monotonic() - start) * 1000)
        self.budget.record(day, agent, cfg.provider, comp.cost_micro_usd)
        if decider != "pinned":
            self.tracker.remember(req.session, user_text, pool)
        return done(200, decider, "ok", mode=mode, pool=pool, hits=hits, comp=comp, ms=ms,
                    shadow=shadow, text=comp.text)
