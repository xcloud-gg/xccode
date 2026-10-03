"""xcbench — benchmark runner (XC-CODE-001 §4.13, §8).

Runs a *routing suite* — short prompts grouped by mode — against each pool and turns the results
into the ``PoolScore`` rows the router's ``pick_pool`` consumes (quality, cost_norm, latency_norm).
Quality is measured against an optional per-prompt reference (``expect``), or a non-empty
completion when no reference is given. Cost comes from the pool's estimated micro-USD, latency
from the measured round trip. Benchmarks always run with memory off: only the model is measured.
"""

from __future__ import annotations

import json
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .xcroute.decide import PoolScore
from .xcroute.provider import OmniRouteProvider


@dataclass(frozen=True)
class SuitePrompt:
    mode: str
    prompt: str
    expect: str | None = None


@dataclass(frozen=True)
class PoolResult:
    pool: str
    quality: float
    cost_micro_usd: int
    latency_ms: float
    passed: int
    total: int


def load_suite(path: Path) -> list[SuitePrompt]:
    """Read a routing suite. A TOML list of ``[[routing]]`` entries: ``mode``, ``prompt``,
    and an optional ``expect`` (substring the completion must contain to count as correct)."""
    data = tomllib.loads(path.read_text())
    out: list[SuitePrompt] = []
    for entry in data.get("routing", []):
        out.append(
            SuitePrompt(
                mode=str(entry["mode"]),
                prompt=str(entry["prompt"]),
                expect=str(entry["expect"]) if entry.get("expect") else None,
            )
        )
    return out


def run_prompt(
    provider: OmniRouteProvider,
    pool: str,
    prompt: SuitePrompt,
    timeout: float = 120.0,
) -> tuple[bool, int]:
    """Run one prompt against a pinned pool. Returns (correct, latency_ms)."""
    messages = [{"role": "user", "content": prompt.prompt}]
    start = time.monotonic()
    completion = provider(pool, messages)
    latency_ms = int((time.monotonic() - start) * 1000)
    if prompt.expect is not None:
        correct = prompt.expect in completion.text
    else:
        correct = bool(completion.text.strip())
    return correct, latency_ms


def run_pool(
    provider: OmniRouteProvider,
    pool: str,
    prompts: list[SuitePrompt],
    est_cost_micro_usd: int,
) -> PoolResult:
    """Run every prompt in the suite against one pool and aggregate the score."""
    passed = 0
    total_latency = 0
    for p in prompts:
        correct, latency = run_prompt(provider, pool, p)
        passed += 1 if correct else 0
        total_latency += latency
    total = len(prompts)
    quality = passed / total if total else 0.0
    avg_latency = total_latency / total if total else 0.0
    return PoolResult(
        pool=pool,
        quality=quality,
        cost_micro_usd=est_cost_micro_usd,
        latency_ms=avg_latency,
        passed=passed,
        total=total,
    )


def normalize(results: list[PoolResult]) -> dict[str, PoolScore]:
    """Normalise cost and latency across pools to 0..1 (min-max; 0 if a single pool)."""
    costs = [r.cost_micro_usd for r in results]
    latencies = [r.latency_ms for r in results]
    cmin, cmax = min(costs), max(costs)
    lmin, lmax = min(latencies), max(latencies)

    def norm(v: float, lo: float, hi: float) -> float:
        if hi <= lo:
            return 0.0
        return (v - lo) / (hi - lo)

    return {
        r.pool: PoolScore(
            quality=r.quality,
            cost_norm=norm(r.cost_micro_usd, cmin, cmax),
            latency_norm=norm(r.latency_ms, lmin, lmax),
        )
        for r in results
    }


def bench(
    provider: OmniRouteProvider,
    prompts: list[SuitePrompt],
    pools: dict[str, int],
) -> dict[str, PoolScore]:
    """Run the suite against every pool and return the score table."""
    results = [run_pool(provider, pool, prompts, cost) for pool, cost in pools.items()]
    return normalize(results)


def scores_to_toml(scores: dict[str, PoolScore]) -> str:
    """Render the score table as a ``[scores]`` TOML block for serve.toml."""
    lines = ["[scores]"]
    for pool, s in sorted(scores.items()):
        lines.append(
            f'{pool} = {{ quality = {s.quality:.3f}, cost_norm = {s.cost_norm:.3f}, '
            f'latency_norm = {s.latency_norm:.3f} }}'
        )
    return "\n".join(lines) + "\n"


def results_to_json(results: list[PoolResult]) -> str:
    return json.dumps([r.__dict__ for r in results], indent=2, sort_keys=True) + "\n"
