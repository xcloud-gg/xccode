import json

import pytest

from tests.test_gate import P3_PLAN, advisor_record
from xccode import cli
from xccode.audit import AuditLog
from xccode.hashing import plan_hash


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ETC", tmp_path / "etc")
    monkeypatch.setattr(cli, "STATE", tmp_path / "state")
    return tmp_path


def test_classify_prints_tier(capsys):
    assert cli.main(["classify", "reboot"]) == 0
    assert capsys.readouterr().out.startswith("P3")
    assert cli.main(["classify", "--", "ls", "-la"]) == 0
    assert capsys.readouterr().out.startswith("P0")


def test_approve_without_a_terminal_is_refused_and_audited(paths, capsys):
    # pytest's stdin is not a tty, which is exactly the agent / background-process case (B-46)
    assert cli.main(["approve", "abcd1234"]) == 4
    assert "terminal" in capsys.readouterr().err
    events = [r["event"] for r in AuditLog(paths / "state" / "audit" / "audit.jsonl").records()]
    assert "approver.refused" in events


def test_enroll_without_a_terminal_is_refused(paths):
    assert cli.main(["approve", "enroll"]) == 4


def test_reset_as_non_root_is_refused(paths, capsys, monkeypatch):
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    assert cli.main(["approve", "reset"]) == 2
    assert "root" in capsys.readouterr().err


def test_submit_opens_a_pending_request_for_a_p3_plan(paths, capsys):
    plan_file = paths / "plan.json"
    plan_file.write_text(json.dumps(P3_PLAN))
    adv = paths / "adv.json"
    adv.write_text(json.dumps(advisor_record(plan_hash(P3_PLAN))))
    assert cli.main(["submit", str(plan_file), "--advisor-record", str(adv)]) == 0
    out = capsys.readouterr().out
    assert "tier P3" in out and "pending" in out


def test_submit_refuses_an_advisor_record_for_another_plan(paths, capsys):
    plan_file = paths / "plan.json"
    plan_file.write_text(json.dumps(P3_PLAN))
    adv = paths / "adv.json"
    adv.write_text(json.dumps(advisor_record("f" * 64)))
    assert cli.main(["submit", str(plan_file), "--advisor-record", str(adv)]) == 2


def test_run_refuses_p3_without_approval(paths, capsys):
    plan_file = paths / "plan.json"
    plan_file.write_text(json.dumps(P3_PLAN))
    adv = paths / "adv.json"
    adv.write_text(json.dumps(advisor_record(plan_hash(P3_PLAN))))
    cli.main(["submit", str(plan_file), "--advisor-record", str(adv)])
    assert cli.main(["run", plan_hash(P3_PLAN)]) == 3
    assert "refused" in capsys.readouterr().err
