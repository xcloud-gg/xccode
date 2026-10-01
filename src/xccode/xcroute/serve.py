"""Assemble a Router from an operator-owned config and run it (XC-DES-001 §6.4.4, §6.5).

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
from .provider import OmniRouteProvider
from .router import PoolConfig, Router

DEFAULT_BASE_URL = "http://127.0.0.1:18128"


def _default_limits() -> Limits:
    return Limits(per_request=1_000_000, per_day=10_000_000)


@dataclass(frozen=True)
class ServeConfig:
    base_url: str = DEFAULT_BASE_URL
    tokens: dict[str, str] = field(default_factory=dict)  # agent -> sha256 hex digest
    pools: dict[str, PoolConfig] = field(default_factory=dict)
    scores: dict[str, PoolScore] = field(default_factory=dict)
    limits: Limits = field(default_factory=_default_limits)
    state_dir: Path = Path("/var/lib/xcloud/xccode")


def load_config(path: Path) -> ServeConfig:
    data = tomllib.loads(path.read_text())
    pools = {
        name: PoolConfig(
            provider=str(meta["provider"]),
            est_cost_micro_usd=int(meta.get("est_cost_micro_usd", 0)),
        )
        for name, meta in data.get("pools", {}).items()
    }
    scores = {
        name: PoolScore(
            quality=float(meta["quality"]),
            cost_norm=float(meta["cost_norm"]),
            latency_norm=float(meta["latency_norm"]),
        )
        for name, meta in data.get("scores", {}).items()
    }
    b = data.get("budget", {})
    limits = Limits(
        per_request=int(b.get("per_request", 1_000_000)),
        per_day=int(b.get("per_day", 10_000_000)),
    )
    return ServeConfig(
        base_url=str(data.get("base_url", DEFAULT_BASE_URL)),
        tokens=dict(data.get("tokens", {})),
        pools=pools,
        scores=scores,
        limits=limits,
        state_dir=Path(str(data.get("state_dir", "/var/lib/xcloud/xccode"))),
    )


def build_router(cfg: ServeConfig) -> Router:
    return Router(
        auth=TokenAuth(cfg.tokens),
        pools=cfg.pools,
        scores=cfg.scores,
        healthy=lambda: set(cfg.pools),
        budget=BudgetGate(cfg.limits),
        provider=OmniRouteProvider(base_url=cfg.base_url),
        events=EventLog(cfg.state_dir / "events.db"),
    )
