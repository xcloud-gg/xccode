"""Budget gate (§5 step 4): per request, per day, per provider, per agent. Integer micro-USD.

Totals are persisted to a JSON file (when `state_path` is given) so an xcroute restart does not
reset the spend for the current UTC day. Without `state_path` the gate is in-memory only, which the
tests use.

The gate fails closed: a state file that exists but cannot be read or parsed refuses every request
until the operator fixes it, rather than silently restarting the cap at zero. A missing file is a
fresh start (no spend today). The in-memory updates and the file write are guarded by a lock so
concurrent FastAPI worker threads cannot corrupt the counters or the state file.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
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
        self._lock = threading.Lock()
        self._day: str | None = None
        self._total = 0
        self._provider: dict[str, int] = {}
        self._agent: dict[str, int] = {}
        self._broken = False  # an existing state file that cannot be trusted: refuse

    def _load(self, day: str) -> tuple[int, dict[str, int], dict[str, int]] | None:
        """Return the persisted spend for `day`, (0, {}, {}) for a fresh day, or None if corrupt."""
        if self.state_path is None or not self.state_path.exists():
            return 0, {}, {}
        try:
            saved = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(saved, dict):
            return None
        if saved.get("day") != day:
            return 0, {}, {}  # a previous day's file: today starts fresh
        if not isinstance(saved.get("total"), int):
            return None
        provider, agent = saved.get("provider"), saved.get("agent")
        if not isinstance(provider, dict) or not isinstance(agent, dict):
            return None
        try:
            return (
                int(saved["total"]),
                {k: int(v) for k, v in provider.items()},
                {k: int(v) for k, v in agent.items()},
            )
        except (TypeError, ValueError):
            return None

    def _persist(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.state_path.parent), prefix="budget-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {
                        "day": self._day,
                        "total": self._total,
                        "provider": self._provider,
                        "agent": self._agent,
                    },
                    f,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        except OSError:
            # A full disk must not fail a request whose completion has already been charged: keep
            # the in-memory total and leave the stale file; the next persist will retry.
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _roll(self, day: str) -> None:
        if day != self._day:
            self._day = day
            loaded = self._load(day)
            if loaded is None:
                self._broken = True
                self._total, self._provider, self._agent = 0, {}, {}
            else:
                self._broken = False
                self._total, self._provider, self._agent = loaded

    def allows(self, day: str, agent: str, provider: str, estimate: int) -> bool:
        with self._lock:
            self._roll(day)
            if self._broken:
                return False  # fail closed: a corrupt state file refuses, never resets
            lim = self.limits
            if estimate > lim.per_request or self._total + estimate > lim.per_day:
                return False
            pcap = lim.per_provider_day.get(provider)
            if pcap is not None and self._provider.get(provider, 0) + estimate > pcap:
                return False
            acap = lim.per_agent_day.get(agent)
            return not (acap is not None and self._agent.get(agent, 0) + estimate > acap)

    def record(self, day: str, agent: str, provider: str, actual: int) -> None:
        with self._lock:
            self._roll(day)
            self._total += actual
            self._provider[provider] = self._provider.get(provider, 0) + actual
            self._agent[agent] = self._agent.get(agent, 0) + actual
            self._persist()
