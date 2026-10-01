import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install" / "install.sh"


def _run(*args, env=None):
    e = {**os.environ, **(env or {})}
    return subprocess.run(["sh", str(INSTALL), *args], capture_output=True, text=True, env=e)


def test_syntax():
    subprocess.run(["bash", "-n", str(INSTALL)], check=True)


def test_requires_operator():
    r = _run("--check")
    assert r.returncode == 2
    assert "--operator" in r.stderr


def test_unknown_argument_fails():
    r = _run("--operator", "marius", "--bogus")
    assert r.returncode == 2
    assert "unknown argument" in r.stderr


def test_check_is_dry_run_and_touches_nothing(tmp_path):
    etc = tmp_path / "etc"
    r = _run(
        "--operator", "marius", "--check",
        env={
            "XCCODE_ETC": str(etc),
            "XCCODE_STATE": str(tmp_path / "state"),
            "XCCODE_OPT": str(tmp_path / "opt"),
            "XCCODE_SYSTEMD": str(tmp_path / "systemd"),
        },
    )
    assert r.returncode == 0
    assert "would:" in r.stdout
    assert "dry run complete" in r.stdout
    # the dry run created nothing under the overridden paths
    assert not etc.exists()
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "opt").exists()
    assert not (tmp_path / "systemd").exists()


def test_check_reflects_existing_files(tmp_path):
    etc = tmp_path / "etc" / "opencode"
    etc.mkdir(parents=True)
    (etc / "opencode.json").write_text("{}\n")
    r = _run(
        "--operator", "marius", "--check",
        env={"XCCODE_ETC": str(tmp_path / "etc")},
    )
    assert "already: dir " in r.stdout
    assert "already: " + str(etc / "opencode.json") in r.stdout
