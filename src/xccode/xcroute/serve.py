"""Assemble a Router from an operator-owned config and run it (XC-CODE-001 §4.4, §4.5).

The public repository ships no tokens, budget caps, or OmniRoute address: they arrive in a TOML
file the installer writes from SOPS. Missing values fall back to safe defaults (no tokens -> every
request is a 401; no pools -> every model is refused).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .auth import TokenAuth
from .budget import BudgetGate, Limits
from .decide import PoolScore
from .events import EventLog
from .memory import OPENVIKING_URL, OpenVikingMemory
from .provider import OmniRouteProvider
from .router import PoolConfig, Router

DEFAULT_BASE_URL = "http://127.0.0.1:18128"


def _default_limits() -> Limits:
    return Limits(per_request=1_000_000, per_day=10_000_000)


@dataclass(frozen=True)
class ServeConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""  # bearer key for the private OmniRoute instance (§4.5)
    openviking_url: str = OPENVIKING_URL  # read-only memory door (§4.7)
    tokens: dict[str, str] = field(default_factory=dict)  # agent -> sha256 hex digest
    pools: dict[str, PoolConfig] = field(default_factory=dict)
    scores: dict[str, PoolScore] = field(default_factory=dict)
    limits: Limits = field(default_factory=_default_limits)
    state_dir: Path = Path("/var/lib/xcloud/xccode")


def _parse_scores(data: dict) -> dict[str, PoolScore]:
    """Read a ``[scores]`` table (serve.toml or the bench state file) into ``PoolScore`` rows."""
    return {
        name: PoolScore(
            quality=float(meta["quality"]),
            cost_norm=float(meta["cost_norm"]),
            latency_norm=float(meta["latency_norm"]),
        )
        for name, meta in data.get("scores", {}).items()
    }


class ScoresFile:
    """Pool scores re-read from ``state_dir/bench/scores.toml`` when it changes (XC-CODE-001 §8).

    The weekly ``xccode bench --apply`` run (User=xccode) cannot reload xcroute.service, so the
    router re-reads the state file instead: a cheap ``stat`` per ``get()`` picks up new scores
    the moment the bench writes them. A missing or unreadable file falls back to the static
    ``serve.toml`` ``[scores]``; a file that turns unreadable after a good read keeps the last
    good scores.
    """

    def __init__(self, path: Path, fallback: dict[str, PoolScore]) -> None:
        self._path = path
        self._fallback = fallback
        self._mtime_ns: int | None = None
        self._scores: dict[str, PoolScore] | None = None

    def get(self) -> dict[str, PoolScore]:
        try:
            mtime_ns = self._path.stat().st_mtime_ns
        except OSError:
            return self._fallback
        if self._scores is not None and mtime_ns == self._mtime_ns:
            return self._scores
        try:
            scores = _parse_scores(tomllib.loads(self._path.read_text()))
        except (OSError, ValueError, KeyError, TypeError):  # TOMLDecodeError is a ValueError
            return self._scores if self._scores is not None else self._fallback
        self._mtime_ns = mtime_ns
        self._scores = scores
        return scores


def load_config(path: Path) -> ServeConfig:
    data = tomllib.loads(path.read_text())
    pools = {
        name: PoolConfig(
            provider=str(meta["provider"]),
            est_cost_micro_usd=int(meta.get("est_cost_micro_usd", 0)),
        )
        for name, meta in data.get("pools", {}).items()
    }
    scores = _parse_scores(data)
    b = data.get("budget", {})
    limits = Limits(
        per_request=int(b.get("per_request", 1_000_000)),
        per_day=int(b.get("per_day", 10_000_000)),
        per_provider_day={k: int(v) for k, v in b.get("per_provider_day", {}).items()},
        per_agent_day={k: int(v) for k, v in b.get("per_agent_day", {}).items()},
    )
    return ServeConfig(
        base_url=str(data.get("base_url", DEFAULT_BASE_URL)),
        api_key=str(data.get("api_key", "")),
        openviking_url=str(data.get("openviking_url", OPENVIKING_URL)),
        tokens=dict(data.get("tokens", {})),
        pools=pools,
        scores=scores,
        limits=limits,
        state_dir=Path(str(data.get("state_dir", "/var/lib/xcloud/xccode"))),
    )


def build_router(cfg: ServeConfig) -> Router:
    scores_file = ScoresFile(cfg.state_dir / "bench" / "scores.toml", cfg.scores)
    return Router(
        auth=TokenAuth(cfg.tokens),
        pools=cfg.pools,
        scores=cfg.scores,
        scores_live=scores_file.get,
        healthy=lambda: set(cfg.pools),
        budget=BudgetGate(cfg.limits, state_path=cfg.state_dir / "budget.json"),
        provider=OmniRouteProvider(base_url=cfg.base_url, api_key=cfg.api_key),
        events=EventLog(cfg.state_dir / "events.db"),
        pending_dir=cfg.state_dir / "learn/pending",
        memory=OpenVikingMemory(base_url=cfg.openviking_url),
    )
