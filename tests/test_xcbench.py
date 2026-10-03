from pathlib import Path

import httpx

from xccode.xcbench import (
    PoolResult,
    load_suite,
    normalize,
    run_pool,
    scores_to_toml,
)
from xccode.xcroute.provider import OmniRouteProvider

REPO = Path(__file__).resolve().parent.parent


def _provider(responses):
    def handler(request):
        import json

        body = json.loads(request.content)
        text = responses.get(body["model"], "ok")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    return OmniRouteProvider(
        "http://x", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_load_suite():
    suite = load_suite(REPO / "etc" / "bench" / "routing.toml")
    assert len(suite) == 4
    assert suite[0].mode == "fast"
    assert suite[0].expect == "pong"
    # the reasoning prompt carries no reference
    assert suite[2].expect is None


def test_run_pool_scores_references():
    provider = _provider({"coding-strong": "pong pong", "fast": "nope"})
    suite = load_suite(REPO / "etc" / "bench" / "routing.toml")
    result = run_pool(provider, "coding-strong", suite, est_cost_micro_usd=100)
    # references that match: "pong" (prompt 1), "4" (prompt 2) — the no-reference ones count
    # as correct when non-empty.
    assert result.pool == "coding-strong"
    assert result.quality > 0.0
    assert result.total == 4
    assert 0 <= result.quality <= 1.0


def test_normalize_single_pool_is_zero_norm():
    results = [PoolResult("fast", 0.9, 10, 42.0, 9, 10)]
    scores = normalize(results)
    assert scores["fast"].cost_norm == 0.0
    assert scores["fast"].latency_norm == 0.0
    assert scores["fast"].quality == 0.9


def test_normalize_scales_between_pools():
    results = [
        PoolResult("cheap", 0.5, 10, 20.0, 5, 10),
        PoolResult("dear", 0.9, 100, 120.0, 9, 10),
    ]
    scores = normalize(results)
    assert scores["cheap"].cost_norm == 0.0
    assert scores["dear"].cost_norm == 1.0
    assert scores["cheap"].latency_norm == 0.0
    assert scores["dear"].latency_norm == 1.0


def test_scores_to_toml_emits_scores_block():
    from xccode.xcroute.decide import PoolScore

    out = scores_to_toml({"fast": PoolScore(0.9, 0.5, 0.4)})
    assert out.startswith("[scores]\n")
    assert "fast = {" in out
    assert "quality = 0.900" in out
