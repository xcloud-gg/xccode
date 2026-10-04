from __future__ import annotations

import json

from xccode.xcroute.learn import digest_id, intake, verify

_CONTENT = ("source", "repo", "task", "files_touched", "commands", "outcome", "notes")


def _digest(**overrides) -> dict:
    content = {
        "source": "thoughts",
        "repo": "xccode",
        "task": "fix the thing",
        "files_touched": ["src/a.py"],
        "commands": ["pytest"],
        "outcome": "tests pass",
        "notes": "reviewed",
    }
    content.update(overrides)
    return {"id": digest_id(content), "collected_at": "2026-10-04T00:00:00Z", **content}


def _rehash(d: dict) -> dict:
    d["id"] = digest_id({k: d[k] for k in _CONTENT})
    return d


def test_valid_digest_verifies_clean():
    d = _digest()
    assert verify(d, set()) == []


def test_intake_writes_and_is_idempotent(tmp_path):
    d = _digest()
    queued, problems = intake(tmp_path, d)
    assert queued and problems == []
    assert (tmp_path / f"{d['id']}.json").exists()
    # second intake of the same digest is a duplicate (already pending)
    queued2, problems2 = intake(tmp_path, d)
    assert not queued2 and "duplicate" in problems2


def test_missing_required_field_is_rejected():
    d = _digest()
    del d["task"]
    assert "missing or empty task" in verify(d, set())


def test_id_mismatch_is_rejected():
    d = _digest()
    d["task"] = "changed after hashing"
    assert "id does not match content" in verify(d, set())


def test_secret_bearing_digest_is_rejected():
    # Build the secret at runtime so the shape never appears as a literal (ci_checks secret scan).
    secret = "sk-ant-" + "a" * 40
    d = _rehash(_digest())
    d["notes"] = f"key is {secret}"
    assert any("secret in notes" in p for p in verify(d, set()))


def test_oversized_digest_is_rejected():
    d = _rehash(_digest(notes="x" * 100_000))
    assert "oversized" in verify(d, set())


def test_intake_preserves_exact_digest(tmp_path):
    d = _digest()
    intake(tmp_path, d)
    back = json.loads((tmp_path / f"{d['id']}.json").read_text())
    assert back == d
