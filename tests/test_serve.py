import os
from pathlib import Path

from xccode.xcroute.serve import build_router, load_config

POOLS = ("coding-strong", "coding-fast", "reasoning", "fast")


def _write_config(tmp_path: Path) -> Path:
    digest = "ab" * 32  # 64 hex chars
    cfg = tmp_path / "serve.toml"
    cfg.write_text(
        'base_url = "http://127.0.0.1:18128"\n'
        'api_key = "test-key"\n'
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
    assert cfg.api_key == "test-key"
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


def _write_state_scores(tmp_path: Path, quality: float) -> Path:
    scores_file = tmp_path / "bench" / "scores.toml"
    scores_file.parent.mkdir(parents=True, exist_ok=True)
    scores_file.write_text(
        "[scores]\n"
        f'coding-strong = {{ quality = {quality}, cost_norm = 0.0, latency_norm = 0.0 }}\n'
    )
    return scores_file


def test_live_scores_fall_back_to_serve_toml_without_a_state_file(tmp_path):
    router = build_router(load_config(_write_config(tmp_path)))
    assert router.scores_live is not None
    assert router.scores_live()["coding-strong"].quality == 0.9  # serve.toml [scores]


def test_live_scores_state_file_overrides_serve_toml(tmp_path):
    router = build_router(load_config(_write_config(tmp_path)))
    _write_state_scores(tmp_path, 0.1)
    assert router.scores_live()["coding-strong"].quality == 0.1


def test_live_scores_reloaded_when_the_state_file_changes(tmp_path):
    router = build_router(load_config(_write_config(tmp_path)))
    scores_file = _write_state_scores(tmp_path, 0.1)
    assert router.scores_live()["coding-strong"].quality == 0.1
    scores_file.write_text(
        "[scores]\n"
        'coding-strong = { quality = 0.7, cost_norm = 0.0, latency_norm = 0.0 }\n'
    )
    os.utime(scores_file, ns=(scores_file.stat().st_atime_ns,
                              scores_file.stat().st_mtime_ns + 1_000_000_000))
    assert router.scores_live()["coding-strong"].quality == 0.7


def test_live_scores_unreadable_state_file_keeps_last_good(tmp_path):
    router = build_router(load_config(_write_config(tmp_path)))
    scores_file = _write_state_scores(tmp_path, 0.1)
    assert router.scores_live()["coding-strong"].quality == 0.1
    scores_file.write_text("not toml [[[")
    os.utime(scores_file, ns=(scores_file.stat().st_atime_ns,
                              scores_file.stat().st_mtime_ns + 1_000_000_000))
    assert router.scores_live()["coding-strong"].quality == 0.1


def test_live_scores_invalid_state_file_from_the_start_falls_back(tmp_path):
    router = build_router(load_config(_write_config(tmp_path)))
    scores_file = tmp_path / "bench" / "scores.toml"
    scores_file.parent.mkdir(parents=True)
    scores_file.write_text("not toml [[[")
    assert router.scores_live()["coding-strong"].quality == 0.9  # serve.toml [scores]
