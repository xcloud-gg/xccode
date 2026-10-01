import hashlib
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VERIFY = REPO / "install" / "verify-release.sh"


def _fake_gpgv(tmp_path, exit_code):
    f = tmp_path / "gpgv"
    f.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    f.chmod(0o755)
    return str(f)


def _release(tmp_path, content):
    payload = tmp_path / "xccode-release.tar.gz"
    payload.write_bytes(content)
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{hashlib.sha256(content).hexdigest()}  xccode-release.tar.gz\n")
    sig = tmp_path / "SHA256SUMS.sig"
    sig.write_text("sig\n")
    key = tmp_path / "signing-key.asc"
    key.write_text("key\n")
    return payload, sums, sig, key


def _run(tmp_path, payload, sums, sig, key, env=None):
    e = {**os.environ, **(env or {})}
    return subprocess.run(
        ["sh", str(VERIFY), str(payload), str(sums), str(sig), str(key)],
        capture_output=True, text=True, env=e,
    )


def test_syntax():
    subprocess.run(["bash", "-n", str(VERIFY)], check=True)


def test_verifies_ok(tmp_path):
    p, s, sig, k = _release(tmp_path, b"hello")
    r = _run(tmp_path, p, s, sig, k, env={"GPGV": _fake_gpgv(tmp_path, 0)})
    assert r.returncode == 0
    assert "ok" in r.stdout


def test_checksum_mismatch_refused(tmp_path):
    p, s, sig, k = _release(tmp_path, b"hello")
    p.write_bytes(b"tampered")  # no longer matches SHA256SUMS
    r = _run(tmp_path, p, s, sig, k, env={"GPGV": _fake_gpgv(tmp_path, 0)})
    assert r.returncode == 1
    assert "checksum mismatch" in r.stderr


def test_signature_failure_refused(tmp_path):
    p, s, sig, k = _release(tmp_path, b"hello")
    r = _run(tmp_path, p, s, sig, k, env={"GPGV": _fake_gpgv(tmp_path, 1)})
    assert r.returncode == 1
    assert "signature" in r.stderr
