import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run_launcher(tmp_path: Path, env: dict[str, str]) -> Path:
    fake = tmp_path / "opencode"
    body = '#!/bin/sh\necho "$OPENCODE_CONFIG_DIR|$OPENCODE_DISABLE_AUTOUPDATE" > "$OUT"\n'
    fake.write_text(body)
    fake.chmod(0o755)
    out = tmp_path / "env.txt"
    e = {
        **os.environ,
        "XCCODE_OPENCODE": str(fake),
        "OUT": str(out),
        "HOME": str(tmp_path),
        **env,
    }
    subprocess.run(["sh", "install/xcc"], env=e, cwd=REPO, check=True)
    return out


def test_launcher_sets_config_dir_and_autoupdate(tmp_path):
    out = _run_launcher(tmp_path, {})
    assert out.read_text().strip() == f"{tmp_path}/.config/xccode/opencode|1"


def test_launcher_passes_arguments_through(tmp_path):
    fake = tmp_path / "opencode"
    fake.write_text('#!/bin/sh\necho "$@" > "$OUT"\n')
    fake.chmod(0o755)
    out = tmp_path / "args.txt"
    e = {
        **os.environ,
        "XCCODE_OPENCODE": str(fake),
        "OUT": str(out),
        "HOME": str(tmp_path),
    }
    argv = ["sh", "install/xcc", "run", "--model", "coding-strong"]
    subprocess.run(argv, env=e, cwd=REPO, check=True)
    assert out.read_text().strip() == "run --model coding-strong"


def test_shared_opencode_settings_are_operator_owned_minimal():
    cfg = (REPO / "etc" / "opencode" / "opencode.json").read_text()
    assert '"share": "disabled"' in cfg
    assert '"autoupdate": false' in cfg
