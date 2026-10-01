import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "ci_checks", Path(__file__).resolve().parent.parent / "tools" / "ci_checks.py"
)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def labels(text, extra=()):
    return ci.scan_text("f", text, list(extra))


def test_clean_text_passes():
    assert labels("hello 192.0.2.7 example.com 127.0.0.1 0.0.0.0") == []


def test_private_and_mesh_addresses_are_found():
    parts = [
        ("10", "1", "2", "3"),
        ("192", "168", "0", "9"),
        ("172", "16", "5", "5"),
        ("100", "70", "1", "1"),
    ]
    for p in parts:
        assert labels("host " + ".".join(p)), p


def test_internal_suffix_is_found():
    assert labels("curl https://route.aios." + "internal/x")
    assert labels("baldr.xcloud." + "xc0")


def test_secret_shapes_are_found():
    assert labels("-----BEGIN OPENSSH " + "PRIVATE KEY-----")
    assert labels("token = '" + "a" * 20 + "'")
    assert labels("AKIA" + "A" * 16)


def test_extra_names_come_from_the_environment_list():
    assert labels("the host odin is here", ["odin"])
    assert labels("the host odin is here") == []
