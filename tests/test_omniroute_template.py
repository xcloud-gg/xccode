import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "etc" / "omniroute" / "omniroute.toml"

POOL_NAMES = ["coding-strong", "coding-fast", "reasoning", "fast"]
ROUTE_TABLE = {
    "fast": ("opencode-zen", "deepseek-v4-flash"),
    "coding-fast": ("opencode-zen", "gpt-5.6-sol"),
    "coding-strong": ("opencode-zen", "kimi-k3"),
    "reasoning": ("opencode-zen", "deepseek-v4-pro"),
}


def _load() -> dict:
    return tomllib.loads(TEMPLATE.read_text())


def test_template_parses():
    assert _load()


def test_loopback_bind_and_port():
    server = _load()["server"]
    assert server["bind"] == "127.0.0.1"
    assert server["port"] == 18128


def test_four_pools_two_providers_each():
    pools = _load()["pools"]
    assert [p["name"] for p in pools] == POOL_NAMES
    assert all(p["providers"] == 2 for p in pools)


def test_own_compression_disabled():
    assert _load()["compression"]["enabled"] is False


def test_default_routes_use_opencode_zen():
    routes = _load()["routes"]
    assert {r["pool"]: (r["provider"], r["model"]) for r in routes} == ROUTE_TABLE
    assert all(r["provider"] == "opencode-zen" for r in routes)


def test_template_carries_no_provider_credentials():
    # The public repository never ships a key, credential, or endpoint (XC-CODE-001 §15.3): the
    # template only describes structure and leaves providers to the operator.
    text = TEMPLATE.read_text()
    assert "sk-" not in text
    assert "http" not in text.lower()
