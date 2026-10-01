"""Token authentication: a token maps to one agent name. Only digests are stored."""

from __future__ import annotations

import hashlib
import hmac

AGENTS = ("opencode", "hermes", "dsh-bench", "dsh-job", "openviking")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenAuth:
    def __init__(self, digests: dict[str, str]) -> None:
        """`digests` maps agent name to the sha256 hex digest of its token."""
        unknown = set(digests) - set(AGENTS)
        if unknown:
            raise ValueError(f"unknown agents: {sorted(unknown)}")
        self._digests = dict(digests)

    def agent_for(self, bearer: str | None) -> str | None:
        if not bearer:
            return None
        d = digest(bearer)
        found = None
        for agent, expected in self._digests.items():  # no early exit: constant work per call
            if hmac.compare_digest(d, expected):
                found = agent
        return found
