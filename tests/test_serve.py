from pathlib import Path

from xccode.xcroute.serve import build_router, load_config

POOLS = ("coding-strong", "coding-fast", "reasoning", "fast")


def _write_config(tmp_path: Path) -> Path:
    digest = "ab" * 32  # 64 hex chars
    cfg = tmp_path / "serve.toml"
    cfg.write_text(
        'base_url = "http://127.0.0.1:18128"\n'
        f'tokens = {{ opencode = "{digest}" }}\n'
        f'state_dir = "{tmp_path}"\n'
        "\n[pools]\n"
        'coding-strong = { provider = "anthropic", est_cost_micro_usd = 100 }\n'
        'coding-fast = { provider = "openai", est_cost_micro_usd = 50 }\n'
        'reasoning = { provider = "anthropic", est_cost_micro_usd = 200 }\n'
        'fast = { provider = "openai", est_cost_micro_usd = 10 }\n'
        "\n[scores]\n"
        'coding-strong = { quality = 0.9, cost_norm = 0.5, latency_norm = 0.4 }\n'
        "\n[budget]\n"
        "per_request = 500000\n"
        "per_day = 20000000\n"
    )
    return cfg


def test_load_config(tmp_path):
    cfg = load_config(_write_config(tmp_path))
    assert cfg.base_url == "http://127.0.0.1:18128"
    assert set(cfg.pools) == set(POOLS)
    assert cfg.pools["coding-strong"].provider == "anthropic"
    assert cfg.pools["coding-strong"].est_cost_micro_usd == 100
    assert cfg.scores["coding-strong"].quality == 0.9
    assert cfg.limits.per_request == 500000
    assert cfg.limits.per_day == 20000000
    assert set(cfg.tokens) == {"opencode"}


def test_missing_sections_fall_back(tmp_path):
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    cfg = load_config(empty)
    assert cfg.base_url == "http://127.0.0.1:18128"
    assert cfg.pools == {}
    assert cfg.tokens == {}


def test_build_router_constructs(tmp_path):
    router = build_router(load_config(_write_config(tmp_path)))
    assert router.auth.agent_for(None) is None
    assert set(router.pools) == set(POOLS)
    assert router.healthy() == set(POOLS)
