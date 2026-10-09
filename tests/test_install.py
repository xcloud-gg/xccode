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


def test_check_shows_fetch_when_release_given(tmp_path):
    r = _run(
        "--operator", "marius", "--check", "--release", "v1.0.0",
        env={"XCCODE_ETC": str(tmp_path / "etc")},
    )
    assert "would: fetch + verify release v1.0.0" in r.stdout


def test_check_skips_fetch_without_release(tmp_path):
    r = _run(
        "--operator", "marius", "--check",
        env={"XCCODE_ETC": str(tmp_path / "etc")},
    )
    assert "skip: no --release" in r.stdout


def test_check_shows_service_install(tmp_path):
    r = _run(
        "--operator", "marius", "--check",
        env={"XCCODE_SYSTEMD": str(tmp_path / "systemd")},
    )
    assert f"would: install {tmp_path / 'systemd' / 'xcroute.service'}" in r.stdout


def test_check_shows_venv_when_release_given(tmp_path):
    r = _run(
        "--operator", "marius", "--check", "--release", "v1.0.0",
        env={"XCCODE_OPT": str(tmp_path / "opt")},
    )
    assert "would: create venv + pip install xccode" in r.stdout


def test_check_shows_launcher_install(tmp_path):
    r = _run(
        "--operator", "marius", "--check",
        env={"XCCODE_ETC": str(tmp_path / "etc")},
    )
    assert "would: install /home/marius/.local/bin/xcc" in r.stdout


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


def test_check_shows_update_for_changed_tracked_asset(tmp_path):
    # copy_file must overwrite a tracked asset when its content changed (not skip-if-exists), so an
    # upgrade actually applies. A changed systemd unit therefore shows "would: update".
    sysd = tmp_path / "systemd"
    sysd.mkdir(parents=True)
    (sysd / "xcroute.service").write_text("# stale unit\n")
    r = _run(
        "--operator", "marius", "--check",
        env={"XCCODE_SYSTEMD": str(sysd)},
    )
    assert f"would: update {sysd / 'xcroute.service'}" in r.stdout


def test_check_shows_already_for_unchanged_tracked_asset(tmp_path):
    # copy_file is still a no-op when the installed asset is byte-identical to the tracked source.
    sysd = tmp_path / "systemd"
    sysd.mkdir(parents=True)
    unit_src = REPO / "install" / "units" / "xcroute.service"
    (sysd / "xcroute.service").write_text(unit_src.read_text())
    r = _run(
        "--operator", "marius", "--check",
        env={"XCCODE_SYSTEMD": str(sysd)},
    )
    assert f"already: {sysd / 'xcroute.service'}" in r.stdout


def test_check_skips_restore_without_repo(tmp_path):
    r = _run("--operator", "marius", "--check", env={"XCCODE_ETC": str(tmp_path / "etc")})
    assert "skip: no --restore" in r.stdout


def test_check_shows_restore_with_repo(tmp_path):
    r = _run(
        "--operator", "marius", "--check", "--restore", "/backup/xccode-repo",
        env={"XCCODE_STATE": str(tmp_path / "state")},
    )
    assert "would: restic restore /backup/xccode-repo latest" in r.stdout


def test_check_shows_backup_unit_and_nft_unit(tmp_path):
    r = _run(
        "--operator", "marius", "--check",
        env={"XCCODE_SYSTEMD": str(tmp_path / "systemd")},
    )
    assert f"would: install {tmp_path / 'systemd' / 'xccode-backup.timer'}" in r.stdout
    assert f"would: install {tmp_path / 'systemd' / 'xccode-nft.service'}" in r.stdout


def test_check_shows_omniroute_service_and_runtime(tmp_path):
    r = _run(
        "--operator", "marius", "--check",
        env={
            "XCCODE_OPT": str(tmp_path / "opt"),
            "XCCODE_SYSTEMD": str(tmp_path / "systemd"),
        },
    )
    assert f"would: install {tmp_path / 'systemd' / 'omniroute.service'}" in r.stdout
    assert "would: install node 24.14.1" in r.stdout
    assert "would: fetch + build OmniRoute" in r.stdout


def test_check_shows_m5_components(tmp_path):
    r = _run(
        "--operator", "marius", "--check",
        env={
            "XCCODE_OPT": str(tmp_path / "opt"),
            "XCCODE_SYSTEMD": str(tmp_path / "systemd"),
        },
    )
    assert "would: npm install -g @deepseek-ai/dsh@0.2.0-rc.2" in r.stdout
    assert "would: pip install openviking==0.4.23" in r.stdout
    assert "would: install hermes v2026.9.24 (own venv)" in r.stdout
    # TEI's sha256 is empty in the development versions.lock (filled at release time),
    # so the step correctly skips until the binary is shipped.
    assert "tei" in r.stdout.lower()  # mentioned (skip or would)
    assert f"would: install {tmp_path / 'systemd' / 'openviking.service'}" in r.stdout
    assert f"would: install {tmp_path / 'systemd' / 'tei.service'}" in r.stdout


def test_check_shows_opencode_npm_install(tmp_path):
    r = _run(
        "--operator", "marius", "--check",
        env={"XCCODE_OPT": str(tmp_path / "opt")},
    )
    # opencode is installed from the @opencode/cli npm package at the pinned version
    # (either "would: npm install ..." on a fresh host or "already: opencode ...").
    assert "opencode" in r.stdout.lower()
    assert "2.0.26" in r.stdout


def test_check_wires_xcc_env_handoff(tmp_path):
    r = _run(
        "--operator", "marius", "--check",
        env={"XCCODE_ETC": str(tmp_path / "etc")},
    )
    # step_xcc_env follows step_tokens; with no tokens generated yet it reports "skip: no tokens".
    assert "no tokens" in r.stdout


def test_check_shows_timers(tmp_path):
    r = _run(
        "--operator", "marius", "--check",
        env={"XCCODE_SYSTEMD": str(tmp_path / "systemd")},
    )
    assert f"would: install {tmp_path / 'systemd' / 'xccode-bench.timer'}" in r.stdout
    assert f"would: install {tmp_path / 'systemd' / 'xccode-learn.timer'}" in r.stdout


def test_check_shows_opencode_serve_unit(tmp_path):
    r = _run(
        "--operator", "marius", "--check",
        env={"XCCODE_ETC": str(tmp_path / "etc")},
    )
    assert "would: write /etc/credstore/opencode-serve" in r.stdout
    assert "would: install /home/marius/.config/systemd/user/opencode-serve.service" in r.stdout
