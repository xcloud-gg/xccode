"""Budget gate (§6.5 step 4): per request, per day, per provider, per agent. Integer micro-USD."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Limits:
    per_request: int
    per_day: int
    per_provider_day: dict[str, int] = field(default_factory=dict)
    per_agent_day: dict[str, int] = field(default_factory=dict)  # e.g. hermes: 500_000 per night


class BudgetGate:
    """Hard cap. Spend is counted per UTC day; an estimate that would cross any limit is refused."""

    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self._day: str | None = None
        self._total = 0
        self._provider: dict[str, int] = {}
        self._agent: dict[str, int] = {}

    def _roll(self, day: str) -> None:
        if day != self._day:
            self._day, self._total, self._provider, self._agent = day, 0, {}, {}

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
