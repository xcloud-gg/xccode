"""Tests for xccode.ops — the §4.16 operator commands (status/secrets/review/keep/report/
upgrade/export)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from xccode import ops
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


# --- status / secrets surface ---


def test_status_returns_string_with_services_line():
    text = ops.status()
    assert "status:" in text and "xcroute" in text


def test_secrets_requires_a_known_action():
    class A:
        action = "bogus"
        name = "x"
    assert ops.cmd_secrets(A()) == 2
