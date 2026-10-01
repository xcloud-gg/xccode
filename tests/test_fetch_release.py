import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FETCH = REPO / "install" / "fetch-release.sh"


def _fake_downloader(tmp_path):
    f = tmp_path / "dl"
    f.write_text(
        "#!/bin/sh\n"
        "out=''; url=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -o) out=\"$2\"; shift 2 ;;\n"
        "    -*) shift ;;\n"
        "    *) url=\"$1\"; shift ;;\n"
        "  esac\n"
        "done\n"
        "cp \"$SRC/$(basename \"$url\")\" \"$out\"\n"
    )
    f.chmod(0o755)
    return str(f)


def _fake_gpgv_ok(tmp_path):
    f = tmp_path / "gpgv"
    f.write_text("#!/bin/sh\nexit 0\n")
    f.chmod(0o755)
    return str(f)


def _tarball_bytes():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"release payload\n"
        info = tarfile.TarInfo(name="xccode-release/payload.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _release_assets(tmp_path, tag, content):
    src = tmp_path / "src"
    src.mkdir()
    (src / f"xccode-{tag}.tar.gz").write_bytes(content)
    (src / "SHA256SUMS").write_text(f"{hashlib.sha256(content).hexdigest()}  xccode-{tag}.tar.gz\n")
    (src / "SHA256SUMS.sig").write_text("sig\n")
    return src


def _run(tmp_path, tag, out, src, env_extra=None):
    env = {
        **os.environ,
        "DOWNLOADER": _fake_downloader(tmp_path),
        "SRC": str(src),
        "GPGV": _fake_gpgv_ok(tmp_path),
        **(env_extra or {}),
    }
    return subprocess.run(
        ["sh", str(FETCH), "--release", tag, "--url", "https://release.invalid",
         "--out", str(out), "--keyring", str(tmp_path / "signing-key.asc")],
        capture_output=True, text=True, env=env,
    )


def test_syntax():
    subprocess.run(["bash", "-n", str(FETCH)], check=True)


def test_requires_args():
    r = subprocess.run(["sh", str(FETCH)], capture_output=True, text=True)
    assert r.returncode == 2


def test_check_is_dry_run(tmp_path):
    out = tmp_path / "out"
    env = {**os.environ, "DOWNLOADER": _fake_downloader(tmp_path)}
    r = subprocess.run(
        ["sh", str(FETCH), "--release", "v1.0.0", "--url", "https://release.invalid",
         "--out", str(out), "--keyring", str(tmp_path / "k"), "--check"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0
    assert "would: fetch" in r.stdout
    assert not out.exists()


def test_fetch_verify_extract(tmp_path):
    tag = "v1.0.0"
    src = _release_assets(tmp_path, tag, _tarball_bytes())
    out = tmp_path / "out"
    r = _run(tmp_path, tag, out, src)
    assert r.returncode == 0, r.stderr
    assert "extracted" in r.stdout
    assert (out / "xccode-release" / "payload.txt").read_text() == "release payload\n"


def test_tampered_release_refused(tmp_path):
    tag = "v1.0.0"
    src = _release_assets(tmp_path, tag, _tarball_bytes())
    (src / f"xccode-{tag}.tar.gz").write_bytes(b"tampered")
    out = tmp_path / "out"
    r = _run(tmp_path, tag, out, src)
    assert r.returncode == 1
    assert "checksum mismatch" in r.stderr
