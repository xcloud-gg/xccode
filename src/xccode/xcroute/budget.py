"""Budget gate (§5 step 4): per request, per day, per provider, per agent. Integer micro-USD.

Totals are persisted to a JSON file (when `state_path` is given) so an xcroute restart does not
reset the spend for the current UTC day. Without `state_path` the gate is in-memory only, which the
tests use.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Limits:
    per_request: int
    per_day: int
    per_provider_day: dict[str, int] = field(default_factory=dict)
    per_agent_day: dict[str, int] = field(default_factory=dict)  # e.g. hermes: 500_000 per night


class BudgetGate:
    """Hard cap. Spend is counted per UTC day; an estimate that would cross any limit is refused."""

    def __init__(self, limits: Limits, state_path: Path | str | None = None) -> None:
        self.limits = limits
        self.state_path = Path(state_path) if state_path is not None else None
        self._day: str | None = None
        self._total = 0
        self._provider: dict[str, int] = {}
        self._agent: dict[str, int] = {}

    def _load(self, day: str) -> tuple[int, dict[str, int], dict[str, int]]:
        if self.state_path is None or not self.state_path.exists():
            return 0, {}, {}
        try:
            saved = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return 0, {}, {}
        if not isinstance(saved, dict) or saved.get("day") != day:
            return 0, {}, {}
        provider = {k: int(v) for k, v in saved.get("provider", {}).items()}
        agent = {k: int(v) for k, v in saved.get("agent", {}).items()}
        return int(saved.get("total", 0)), provider, agent

    def _persist(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "day": self._day,
                    "total": self._total,
                    "provider": self._provider,
                    "agent": self._agent,
                }
            )
        )
        os.replace(tmp, self.state_path)

    def _roll(self, day: str) -> None:
        if day != self._day:
            self._day = day
            self._total, self._provider, self._agent = self._load(day)

    def allows(self, day: str, agent: str, provider: str, estimate: int) -> bool:
        self._roll(day)
        lim = self.limits
        if estimate > lim.per_request or self._total + estimate > lim.per_day:
            return False
        pcap = lim.per_provider_day.get(provider)
        if pcap is not None and self._provider.get(provider, 0) + estimate > pcap:
            return False
        acap = lim.per_agent_day.get(agent)
        return not (acap is not None and self._agent.get(agent, 0) + estimate > acap)

    def record(self, day: str, agent: str, provider: str, actual: int) -> None:
        self._roll(day)
        self._total += actual
        self._provider[provider] = self._provider.get(provider, 0) + actual
        self._agent[agent] = self._agent.get(agent, 0) + actual
        self._persist()
