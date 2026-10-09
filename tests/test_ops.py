"""Tests for xccode.ops — the §4.16 operator commands (status/secrets/review/keep/report/
upgrade/export)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from xccode import config, ops
from xccode.versions import COMPONENTS, Pin

# --- keep ---


def _seed_events_db(state: Path, n=2):
    state.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state / "events.db") as c:
        c.execute(
            "CREATE TABLE routing_events (id INTEGER PRIMARY KEY, ts TEXT, agent TEXT, "
            "decider TEXT, pool TEXT, outcome TEXT, body TEXT)"
        )
        for i in range(n):
            c.execute(
                "INSERT INTO routing_events (ts, agent, decider, pool, outcome, body) "
                "VALUES (?,?,?,?,?,?)",
                (f"2026-10-0{i+1}T00:00:00Z", "opencode", "rules", "coding-strong",
                 "ok", json.dumps({"xc_agent_role": "opencoder", "cost_micro_usd": 60,
                                    "tokens_in": 1, "tokens_out": 2})),
            )


def test_keep_saves_a_turn_with_expiry(tmp_path):
    _seed_events_db(tmp_path)
    assert ops.keep("1", tmp_path) == 0
    entry = json.loads((tmp_path / "kept" / "1.json").read_text())
    assert entry["request_id"] == 1 and entry["expires_after_days"] == 180
    assert entry["body"]["xc_agent_role"] == "opencoder"


def test_keep_rejects_unknown_and_bad_ids(tmp_path):
    _seed_events_db(tmp_path)
    assert ops.keep("nope", tmp_path) == 2
    assert ops.keep("99", tmp_path) == 1
    assert not (tmp_path / "kept").exists()


# --- review ---


def _seed_review(state: Path):
    d = state / "learn" / "review"
    d.mkdir(parents=True)
    (d / "abc123.json").write_text(json.dumps({"kind": "rule", "content": "always test"}))


def test_review_lists_queue(tmp_path, capsys):
    _seed_review(tmp_path)
    assert ops.review_list(tmp_path) == 0
    out = capsys.readouterr().out
    assert "abc123" in out and "rule" in out


def test_review_approve_moves_to_approved(tmp_path, capsys):
    _seed_review(tmp_path)
    assert ops.review_decide("abc123", approve=True, state_dir=tmp_path) == 0
    assert (tmp_path / "learn" / "approved" / "abc123.json").is_file()
    assert not (tmp_path / "learn" / "review" / "abc123.json").exists()


def test_review_reject_and_ambiguous(tmp_path):
    _seed_review(tmp_path)
    (tmp_path / "learn" / "review" / "abc999.json").write_text("{}")
    # ambiguous and unknown both refuse without touching anything (advisor C3: exact/prefix only)
    assert ops.review_decide("abc", approve=False, state_dir=tmp_path) == 1
    assert ops.review_decide("zzz", approve=False, state_dir=tmp_path) == 1
    assert (tmp_path / "learn" / "review" / "abc123.json").exists()
    assert (tmp_path / "learn" / "review" / "abc999.json").exists()
    assert not (tmp_path / "learn" / "approved").exists()


# --- report ---


def test_report_summarises_events_and_queues(tmp_path):
    _seed_events_db(tmp_path)
    (tmp_path / "learn" / "pending").mkdir(parents=True)
    (tmp_path / "learn" / "pending" / "p.json").write_text("{}")
    (tmp_path / "learn" / "review").mkdir(parents=True)
    (tmp_path / "learn" / "review" / "r.json").write_text("{}")
    text = ops.report(tmp_path, days=30)
    assert "requests: 2 (0 refused/failed)" in text
    assert "by agent:  opencode=2" in text
    assert "1 pending · 1 awaiting review" in text
    assert "kept turns:" in text


def test_report_tolerates_no_events_db(tmp_path):
    text = ops.report(tmp_path)
    assert "no events db" in text


# --- upgrade ---


def _full_pins() -> dict[str, Pin]:
    return {name: Pin(name, "1.0.0", "x" * 40) for name in COMPONENTS}


def test_upgrade_shows_diff_and_only_writes_with_apply(tmp_path, capsys):
    lock = tmp_path / "versions.lock"
    lines = []
    for name in COMPONENTS:
        lines.append(f"[{name}]\nversion = \"2.0.0\"\n")
        from xccode.versions import COMPONENTS as C
        field = C.get(name)
        if field:
            lines.append(f'{field} = "{"y" * 40}"\n')
    lock.write_text("".join(lines))
    assert ops.upgrade(apply=False, pins_path=lock) == 0
    out = capsys.readouterr().out
    assert "component" in out and "diff shown" in out
    # --apply points the operator at re-running the installer; it never installs anything itself
    assert "install.sh" in out


def test_upgrade_apply_requires_root_before_reading_pins(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ops.os, "geteuid", lambda: 1000)
    missing = tmp_path / "does-not-exist.toml"
    assert ops.upgrade(apply=True, pins_path=missing) == 2
    assert "must run as root" in capsys.readouterr().err
    assert not (tmp_path / "etc").exists()


def test_upgrade_apply_writes_the_canonical_lock_atomically(tmp_path, monkeypatch, capsys):
    candidate = tmp_path / "candidate.lock"
    lines = []
    for name, field in COMPONENTS.items():
        lines.append(f"[{name}]\nversion = \"2.0.0\"\n")
        if field:
            lines.append(f'{field} = "{"y" * 64}"\n')
    candidate.write_text("".join(lines))
    candidate.chmod(0o644)
    etc = tmp_path / "etc"
    etc.mkdir()
    etc.chmod(0o755)
    monkeypatch.setattr(ops, "ETC", etc)
    monkeypatch.setattr(ops, "INSTALLED_LOCK", tmp_path / "not-installed.lock")
    monkeypatch.setattr(ops, "_require_root_for_upgrade", lambda: True)

    assert ops.upgrade(apply=True, pins_path=candidate) == 0
    target = etc / "versions.lock"
    assert target.read_text() == candidate.read_text()
    assert target.stat().st_mode & 0o777 == 0o644
    assert not list(etc.glob(".*.tmp"))
    assert "re-run install.sh" in capsys.readouterr().out


def test_upgrade_apply_rejects_symlinked_pins(tmp_path, monkeypatch, capsys):
    real = tmp_path / "real.lock"
    real.write_text("[component]\nversion = \"1\"\n")
    link = tmp_path / "link.lock"
    link.symlink_to(real)
    etc = tmp_path / "etc"
    etc.mkdir()
    monkeypatch.setattr(ops, "ETC", etc)
    monkeypatch.setattr(ops, "_require_root_for_upgrade", lambda: True)
    assert ops.upgrade(apply=True, pins_path=link) == 1
    assert "cannot read pins" in capsys.readouterr().err
    assert not (etc / "versions.lock").exists()


def test_upgrade_apply_rejects_oversized_and_writable_candidates(tmp_path, monkeypatch, capsys):
    etc = tmp_path / "etc"
    etc.mkdir()
    etc.chmod(0o755)
    monkeypatch.setattr(ops, "ETC", etc)
    monkeypatch.setattr(ops, "_require_root_for_upgrade", lambda: True)
    candidate = tmp_path / "candidate.lock"
    candidate.write_text("x" * 65537)
    candidate.chmod(0o644)
    assert ops.upgrade(apply=True, pins_path=candidate) == 1
    assert "64 KiB" in capsys.readouterr().err

    candidate.write_text("[opencode]\nversion = \"2.0.0\"\n")
    candidate.chmod(0o664)
    assert ops.upgrade(apply=True, pins_path=candidate) == 1
    assert "group- or world-writable" in capsys.readouterr().err


def test_upgrade_apply_refuses_custom_etc_under_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ops.os, "geteuid", lambda: 0)
    for override in (str(tmp_path / "etc"), ".", "relative/path"):
        monkeypatch.setenv("XCCODE_ETC", override)
        assert ops._require_root_for_upgrade() is False
        assert "non-default XCCODE_ETC" in capsys.readouterr().err


def test_empty_path_overrides_match_installer_defaults(monkeypatch):
    monkeypatch.setenv("XCCODE_ETC", "")
    monkeypatch.setenv("XCCODE_STATE", "")
    monkeypatch.setenv("XCCODE_OPT", "")
    monkeypatch.setenv("XCCODE_CREDSTORE", "")
    assert config.configured_path("XCCODE_ETC", ops.DEFAULT_ETC) == ops.DEFAULT_ETC
    assert config.configured_path("XCCODE_STATE", "/var/lib/xcloud/xccode") == Path(
        "/var/lib/xcloud/xccode"
    )
    assert config.configured_path("XCCODE_OPT", "/opt/xcloud/xccode") == Path(
        "/opt/xcloud/xccode"
    )
    assert config.configured_path("XCCODE_CREDSTORE", "/etc/credstore.encrypted") == Path(
        "/etc/credstore.encrypted"
    )
    monkeypatch.setattr(ops.os, "geteuid", lambda: 0)
    assert ops._require_root_for_upgrade() is True


def test_upgrade_apply_preserves_old_lock_when_atomic_replace_fails(
    tmp_path, monkeypatch, capsys
):
    candidate = tmp_path / "candidate.lock"
    lines = []
    for name, field in COMPONENTS.items():
        lines.append(f"[{name}]\nversion = \"2.0.0\"\n")
        if field:
            lines.append(f'{field} = "{"y" * 64}"\n')
    candidate.write_text("".join(lines))
    candidate.chmod(0o644)
    etc = tmp_path / "etc"
    etc.mkdir()
    etc.chmod(0o755)
    old = etc / "versions.lock"
    old.write_text("previous lock\n")
    monkeypatch.setattr(ops, "ETC", etc)
    monkeypatch.setattr(ops, "INSTALLED_LOCK", tmp_path / "not-installed.lock")
    monkeypatch.setattr(ops, "_require_root_for_upgrade", lambda: True)

    def fail_replace(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(ops.os, "replace", fail_replace)
    assert ops.upgrade(apply=True, pins_path=candidate) == 1
    assert old.read_text() == "previous lock\n"
    assert not list(etc.glob(".*.tmp"))
    assert "simulated replace failure" in capsys.readouterr().err


# --- export ---


def test_export_without_pyarrow_falls_back_to_jsonl_and_schema(tmp_path, monkeypatch):
    _seed_events_db(tmp_path)
    out = tmp_path / "export"
    assert ops.export(aios=False, out_dir=out, state_dir=tmp_path) == 0
    jq = (out / "xccode-events.jsonl").read_text().splitlines()
    assert len(jq) == 2 and '"agent": "opencode"' in jq[0]
    schema = json.loads((out / "schema.json").read_text())
    assert schema["fallback"].startswith("jsonl")
    assert any(col["name"] == "xc_repo_class" for col in schema["columns"])


def test_cmd_export_drops_privilege_before_expanding_output_path(tmp_path, monkeypatch):
    calls = []
    home = tmp_path / "xccode-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "root-home"))
    monkeypatch.setenv("XCCODE_EXPORT_DIR", "~/exports")

    def drop():
        calls.append("drop")
        monkeypatch.setenv("HOME", str(home))

    def export(*, aios, out_dir):
        calls.append((aios, out_dir))
        return 0

    monkeypatch.setattr(ops, "_drop_to_xccode", drop)
    monkeypatch.setattr(ops, "export", export)

    class Args:
        out = None
        aios = True

    assert ops.cmd_export(Args()) == 0
    assert calls == ["drop", (True, home / "exports")]


def test_cmd_export_empty_env_uses_state_default(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "STATE", tmp_path / "state")
    monkeypatch.setenv("XCCODE_EXPORT_DIR", "")
    monkeypatch.setattr(ops, "_drop_to_xccode", lambda: None)
    seen = []
    monkeypatch.setattr(
        ops, "export", lambda *, aios, out_dir: seen.append((aios, out_dir)) or 0
    )

    class Args:
        out = None
        aios = False

    assert ops.cmd_export(Args()) == 0
    assert seen == [(False, tmp_path / "state" / "export")]


# --- status / secrets surface ---


def test_status_returns_string_with_services_line():
    text = ops.status()
    assert "status:" in text and "xcroute" in text


def test_secrets_requires_a_known_action():
    class A:
        action = "bogus"
        name = "x"
    assert ops.cmd_secrets(A()) == 2
