from __future__ import annotations

import json

from xccode.collect import (
    Digest,
    collect,
    digest_id,
    read_sessions,
    read_thoughts,
    redact_digest,
    spool,
    to_document,
)
from xccode.hashing import sha256_hex

# Built at runtime so the secret shape never appears as a literal in the public repo
# (ci_checks.py secret scan). Guard-lite's patterns match the assembled value.
CANARY_ANTHROPIC = "sk-ant-" + "a" * 40
CANARY_GITHUB = "ghp_" + "a" * 36


def test_redact_digest_removes_secrets_and_reports_hits():
    d = Digest(
        source="thoughts",
        repo="xccode",
        task="fix the token leak",
        notes=f"key is {CANARY_ANTHROPIC} and the password is hunter2secret123",
    )
    c = redact_digest(d)
    assert CANARY_ANTHROPIC not in c.digest["notes"]
    assert "REDACTED" in c.digest["notes"]
    assert "anthropic-key" in c.hits


def test_redact_digest_preserves_clean_text():
    d = Digest(
        source="opencode-session",
        repo="xccode",
        task="what is a symlink",
        outcome="a symlink is a link",
    )
    c = redact_digest(d)
    assert c.digest["task"] == "what is a symlink"
    assert c.digest["outcome"] == "a symlink is a link"
    assert c.hits == ()


def test_digest_id_is_stable():
    d = {"task": "a", "repo": "r", "files_touched": ["x", "y"]}
    assert digest_id(d) == digest_id({"task": "a", "repo": "r", "files_touched": ["x", "y"]})
    assert len(digest_id(d)) == 64


def test_read_sessions_builds_digests(tmp_path):
    (tmp_path / "s1.json").write_text(
        json.dumps(
            {
                "title": "add feature",
                "messages": [
                    {"role": "user", "content": "do it"},
                    {"role": "assistant", "content": "done"},
                ],
            }
        )
    )
    (tmp_path / "s2.json").write_text(json.dumps({"title": "review", "messages": []}))
    (tmp_path / "notjson.json").write_text("{not json")
    ds = read_sessions(tmp_path)
    assert len(ds) == 2
    assert ds[0].source == "opencode-session"
    assert ds[0].task == "add feature"
    assert ds[0].outcome == "done"


def test_read_thoughts_builds_digests(tmp_path):
    repo = tmp_path / "xccode"
    repo.mkdir()
    (repo / "plan.md").write_text("# plan\nbuild x")
    (repo / "review.md").write_text("looks good")
    ds = read_thoughts(tmp_path)
    assert len(ds) == 2
    assert all(d.source == "thoughts" for d in ds)
    assert {d.task for d in ds} == {"plan", "review"}
    assert {d.repo for d in ds} == {"xccode"}


def test_collect_runs_guard_lite_over_every_source(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "s.json").write_text(
        json.dumps(
            {
                "title": "t",
                "messages": [
                    {
                        "role": "assistant",
                        "content": f"token {CANARY_GITHUB}",
                    }
                ],
            }
        )
    )
    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    out = collect(sessions, thoughts)
    assert len(out) == 1
    assert CANARY_GITHUB not in out[0].digest["outcome"]


def test_spool_and_to_document_round_trip(tmp_path):
    d = Digest(source="thoughts", repo="r", task="t", notes="n")
    c = redact_digest(d)
    doc = to_document(c.digest)
    assert "id" in doc and "collected_at" in doc
    path = spool(doc, tmp_path / "spool")
    assert path.exists()
    back = json.loads(path.read_text())
    assert back["id"] == doc["id"]
    assert back["task"] == "t"


def test_spool_is_idempotent(tmp_path):
    doc = {"id": "abc", "task": "t"}
    p1 = spool(doc, tmp_path)
    p2 = spool(doc, tmp_path)
    assert p1 == p2


def test_sha256_helper_import():
    # hashing helpers used by collect must be importable (regression guard for the module surface)
    assert len(sha256_hex(b"x")) == 64
