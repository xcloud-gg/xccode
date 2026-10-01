"""Turn detection, the rules baseline, the model floor, and pool scoring (§6.5 steps 2, 5, 6)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Pool classes. `rank` is the floor ordering: a code change never goes below coding-strong (E11).
POOL_RANK = {"fast": 0, "coding-fast": 1, "coding-strong": 2, "reasoning": 2}
MODE_POOLS = {
    "coding": ("coding-strong", "coding-fast"),
    "reasoning": ("reasoning",),
    "fast": ("fast",),
}
FLOOR_RANK = POOL_RANK["coding-strong"]
CODE_ROLES = frozenset({"opencoder", "coder", "tester", "build"})  # roles that change code
FAST_MIN_P = 0.8
UNSURE_BAND = 0.1


def message_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Answers:
    """Probabilities of the three yes/no questions (§6.4.5)."""

    multi_step: float  # needs multi-step reasoning?
    code_change: float  # code change (vs. explanation)?
    short: float  # short answer expected?

    def unsure(self) -> bool:
        ps = (self.multi_step, self.code_change, self.short)
        return all(abs(p - 0.5) <= UNSURE_BAND for p in ps)


_CODE = re.compile(
    r"\b(fix|implement|refactor|rename|add|remove|delete|change|update|write|create|patch|"
    r"migrate|rewrite|make it|build|apply)\b", re.I)
_EXPLAIN = re.compile(
    r"\b(explain|what is|what does|why|how does|summari[sz]e|describe|where is)\b", re.I)
_REASON = re.compile(
    r"\b(design|architecture|trade-?offs?|root cause|investigate|plan|prove|compare|"
    r"step by step|debug)\b", re.I)


def rules_answers(message: str) -> Answers:
    """Keyword baseline. Deliberately crude: it is the fallback and the shadow decider."""
    code = bool(_CODE.search(message))
    explain = bool(_EXPLAIN.search(message))
    reason = bool(_REASON.search(message))
    short = explain and not code and len(message) < 200
    return Answers(
        multi_step=0.9 if reason else (0.6 if len(message) > 600 else 0.1),
        code_change=0.9 if code and not explain else (0.6 if code else 0.1),
        short=0.9 if short else 0.1,
    )


def mode_from(a: Answers) -> str:
    if a.code_change >= 0.5:
        return "coding"
    if a.multi_step >= 0.5:
        return "reasoning"
    if a.short >= FAST_MIN_P:
        return "fast"
    return "reasoning"  # an explanation that is neither short nor clearly simple: do not cheap out


@dataclass(frozen=True)
class PoolScore:
    quality: float  # 0..1, latest benchmark
    cost_norm: float  # 0..1
    latency_norm: float  # 0..1


def pick_pool(
    mode: str,
    answers: Answers,
    role: str,
    healthy: set[str],
    scores: dict[str, PoolScore],
    lam: float,
    mu: float,
) -> tuple[str | None, str]:
    """Return (pool, why). None: no healthy pool satisfies the floor; refuse, never downgrade."""
    candidates = [p for p in MODE_POOLS[mode] if p in healthy]
    code_change = answers.code_change >= 0.5 or role in CODE_ROLES
    if code_change:
        candidates = [p for p in candidates if POOL_RANK[p] >= FLOOR_RANK]
        if not candidates:  # mode was reasoning/fast but the turn changes code: lift to the floor
            candidates = [
                p for p in MODE_POOLS["coding"] if p in healthy and POOL_RANK[p] >= FLOOR_RANK
            ]
        why = "floor"
    else:
        if mode == "fast" and answers.short < FAST_MIN_P:
            candidates = [p for p in MODE_POOLS["reasoning"] if p in healthy]
        why = "score"
    if not candidates:
        return None, "no-healthy-pool"

    def score(p: str) -> float:
        s = scores.get(p)
        return float("-inf") if s is None else s.quality - lam * s.cost_norm - mu * s.latency_norm

    return max(candidates, key=lambda p: (score(p), p)), why


class TurnTracker:
    """Decide once per user message; tool-call loops reuse the pool (keeps provider caches warm)."""

    def __init__(self) -> None:
        self._last: dict[str, tuple[str, str]] = {}  # session -> (message hash, pool)

    def sticky(self, session: str, user_text: str) -> str | None:
        last = self._last.get(session)
        return last[1] if last and last[0] == message_hash(user_text) else None

    def remember(self, session: str, user_text: str, pool: str) -> None:
        self._last[session] = (message_hash(user_text), pool)
