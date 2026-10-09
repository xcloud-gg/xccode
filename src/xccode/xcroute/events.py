"""Routing events (§5 step 7): one row per turn. Prompt text is never stored, only its hash."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class RoutingEvent:
    ts: str
    agent: str
    xc_agent_role: str
    decider: str  # rules | pinned | sticky | refused
    mode: str | None
    pool: str | None
    message_hash: str
    guard_hits: tuple[str, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0
    cost_micro_usd: int = 0
    latency_ms: int = 0
    failover: bool = False
    outcome: str = "ok"  # ok | refused:<reason> | error:<kind>
    tool_count: int = 0  # OpenAI tools the caller sent (§7 dsh jobs / §8 routing data)


class EventLog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS routing_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, agent TEXT NOT NULL, "
                "decider TEXT NOT NULL, pool TEXT, outcome TEXT NOT NULL, body TEXT NOT NULL)"
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def write(self, ev: RoutingEvent) -> None:
        body = json.dumps(asdict(ev), sort_keys=True)
        with self._conn() as c:
            c.execute(
                "INSERT INTO routing_events (ts, agent, decider, pool, outcome, body) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ev.ts, ev.agent, ev.decider, ev.pool, ev.outcome, body),
            )

    def all(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT body FROM routing_events ORDER BY id")
            return [json.loads(r[0]) for r in rows]
