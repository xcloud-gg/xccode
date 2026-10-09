import json
from pathlib import Path

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


def test_review_cli_takes_action_before_item(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "xccode.ops.cmd_review", lambda args: seen.append((args.verb, args.item)) or 0
    )
    assert cli.main(["review", "approve", "abc123"]) == 0
    assert seen == [("approve", "abc123")]
    with pytest.raises(SystemExit):
        cli.main(["review", "abc123", "approve"])


def test_cli_empty_path_overrides_use_installer_defaults(monkeypatch):
    monkeypatch.setenv("XCCODE_ETC", "")
    monkeypatch.setenv("XCCODE_STATE", "")
    assert cli.configured_path("XCCODE_ETC", "/etc/xcloud/xccode") == Path(
        "/etc/xcloud/xccode"
    )
    assert cli.configured_path("XCCODE_STATE", "/var/lib/xcloud/xccode") == Path(
        "/var/lib/xcloud/xccode"
    )


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


def _mock_omniroute(monkeypatch, text="pong"):
    import httpx

    from xccode.xcroute.provider import OmniRouteProvider

    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    def fake(base_url, api_key=""):
        return OmniRouteProvider(
            base_url, api_key, client=httpx.Client(transport=httpx.MockTransport(handler))
        )

    monkeypatch.setattr("xccode.xcroute.provider.OmniRouteProvider", fake)


def _bench_args(paths):
    cfg = paths / "serve.toml"
    cfg.write_text(
        "[pools]\n"
        'fast = { provider = "openai", est_cost_micro_usd = 10 }\n'
    )
    suite = paths / "routing.toml"
    suite.write_text('[[routing]]\nmode = "fast"\nprompt = "ping"\nexpect = "pong"\n')
    return ["bench", "--suite", str(suite), "--config", str(cfg)]


def test_bench_prints_scores_without_writing(paths, capsys, monkeypatch):
    _mock_omniroute(monkeypatch)
    assert cli.main(_bench_args(paths)) == 0
    assert capsys.readouterr().out.startswith("[scores]\n")
    assert not (paths / "state" / "bench" / "scores.toml").exists()


def test_bench_apply_writes_state_scores_and_still_prints(paths, capsys, monkeypatch):
    _mock_omniroute(monkeypatch)
    assert cli.main([*_bench_args(paths), "--apply"]) == 0
    assert capsys.readouterr().out.startswith("[scores]\n")
    text = (paths / "state" / "bench" / "scores.toml").read_text()
    assert text.startswith("[scores]\n")
    assert 'fast = { quality = 1.000' in text
