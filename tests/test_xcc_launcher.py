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


def test_launcher_refuses_inside_secret_marked_tree(tmp_path):
    (tmp_path / ".xccode-secret").touch()
    fake = tmp_path / "opencode"
    fake.write_text('#!/bin/sh\ntouch "$OUT"\n')
    fake.chmod(0o755)
    out = tmp_path / "launched.txt"
    e = {
        **os.environ,
        "XCCODE_OPENCODE": str(fake),
        "OUT": str(out),
        "HOME": str(tmp_path),
    }
    r = subprocess.run(["sh", str(REPO / "install" / "xcc")], env=e, cwd=tmp_path)
    assert r.returncode == 1
    assert not out.exists()  # the fake OpenCode never ran


def test_launcher_refuses_when_project_argument_is_marked(tmp_path):
    marked = tmp_path / "marked"
    marked.mkdir()
    (marked / ".xccode-secret").touch()
    fake = tmp_path / "opencode"
    fake.write_text('#!/bin/sh\ntouch "$OUT"\n')
    fake.chmod(0o755)
    out = tmp_path / "launched.txt"
    e = {
        **os.environ,
        "XCCODE_OPENCODE": str(fake),
        "OUT": str(out),
        "HOME": str(tmp_path),
    }
    # run from a clean cwd, passing the marked dir as OpenCode's project argument
    r = subprocess.run(["sh", str(REPO / "install" / "xcc"), str(marked)], env=e, cwd=REPO)
    assert r.returncode == 1
    assert not out.exists()


def test_launcher_refuses_through_symlink_into_marked_tree(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / ".xccode-secret").touch()
    link = tmp_path / "link"
    link.symlink_to(real)
    fake = tmp_path / "opencode"
    fake.write_text('#!/bin/sh\ntouch "$OUT"\n')
    fake.chmod(0o755)
    out = tmp_path / "launched.txt"
    e = {
        **os.environ,
        "XCCODE_OPENCODE": str(fake),
        "OUT": str(out),
        "HOME": str(tmp_path),
    }
    r = subprocess.run(["sh", str(REPO / "install" / "xcc")], env=e, cwd=str(link))
    assert r.returncode == 1
    assert not out.exists()


def test_shared_opencode_settings_are_operator_owned_minimal():
    cfg = (REPO / "etc" / "opencode" / "opencode.json").read_text()
    assert '"share": "disabled"' in cfg
    assert '"autoupdate": false' in cfg


def test_profile_targets_v2_provider_schema():
    """The operator profile targets the released OpenCode V2 (`@opencode/cli`), which uses the
    plural `providers`/`plugins` and nested `mcp.servers`."""
    import json

    cfg = json.loads((REPO / "etc" / "opencode" / "profile.json").read_text())
    assert "providers" in cfg and "provider" not in cfg
    xc = cfg["providers"]["xc"]
    assert xc["package"] == "@opencode/ai/providers/openai-compatible"
    assert xc["settings"]["baseURL"] == "http://127.0.0.1:18080/v1"
    assert xc["env"] == ["XCC_TOKEN"]
    assert xc["models"]["auto"]["modelID"] == "xc/auto"
    assert "plugins" in cfg
    assert "servers" in cfg["mcp"]


def test_launcher_sources_xcc_env_token(tmp_path):
    (tmp_path / ".config" / "xccode").mkdir(parents=True)
    (tmp_path / ".config" / "xccode" / "xcc.env").write_text("XCC_TOKEN=secret-token\n")
    fake = tmp_path / "opencode"
    fake.write_text('#!/bin/sh\necho "$XCC_TOKEN" > "$OUT"\n')
    fake.chmod(0o755)
    out = tmp_path / "token.txt"
    e = {
        **os.environ,
        "XCCODE_OPENCODE": str(fake),
        "OUT": str(out),
        "HOME": str(tmp_path),
    }
    subprocess.run(["sh", "install/xcc"], env=e, cwd=REPO, check=True)
    assert out.read_text().strip() == "secret-token"
