import json

import pytest

from xccode.advisor import AdvisorRecordError, validate_record
from xccode.audit import AuditError, AuditLog
from xccode.gate import GateAborted, GateRefused, execute_plan
from xccode.hashing import canonical, plan_hash
from xccode.store import AdvisorStore, Approval, ApprovalStore, PlanStore, StoreError

P3_PLAN = {"id": "t1", "target": "host-a", "reason": "test", "steps": [{"cmd": "reboot"}]}
P1_PLAN = {
    "id": "t2",
    "target": "host-a",
    "reason": "test",
    "steps": [{"cmd": "systemctl restart x"}],
}


def advisor_record(plan_h, **over):
    rec = {
        "plan_hash": plan_h,
        "pool": "advisor",
        "origin": {"agent": "a", "ref": "handoff-1"},
        "legitimacy_pct": 82,
        "quality_score": 7,
        "evidence": {"audit_log": True, "signed_handoff": True, "task_matches": False},
        "blast_radius": "one host reboots",
        "rollback": "yes",
        "verdict": "ok",
        "sent_payload_hash": "0" * 64,
    }
    rec.update(over)
    return rec


class Env:
    def __init__(self, tmp_path):
        self.now = 1_000_000.0
        self.audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: self.now)
        self.plans = PlanStore(tmp_path / "plans")
        self.advisors = AdvisorStore(tmp_path / "advisor")
        self.approvals = ApprovalStore(tmp_path / "approvals", clock=lambda: self.now)
        self.ran = []
        self.mono = 0.0

    def sleep(self, s):
        self.mono += s

    def run(self, plan_h, **kw):
        return execute_plan(
            plan_h,
            plans=self.plans,
            advisors=self.advisors,
            approvals=self.approvals,
            audit=self.audit,
            runner=lambda step: (self.ran.append(step["cmd"]), 0)[1],
            sleep=self.sleep,
            clock=lambda: self.mono,
            **kw,
        )

    def approve(self, plan_h, adv_h, ttl=300):
        self.approvals.grant(Approval("pid", plan_h, adv_h, int(self.now), int(self.now) + ttl))


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_p1_plan_runs_without_gate(env):
    h = env.plans.save(P1_PLAN)
    assert env.run(h) == [0]
    assert env.ran == ["systemctl restart x"]


def test_p3_without_advisor_record_is_refused(env):
    h = env.plans.save(P3_PLAN)
    with pytest.raises(GateRefused, match="no advisor record"):
        env.run(h)
    assert env.ran == []


def test_p3_with_advisor_but_no_approval_is_refused(env):
    h = env.plans.save(P3_PLAN)
    env.advisors.save(advisor_record(h))
    with pytest.raises(GateRefused, match="no valid approval"):
        env.run(h)
    assert env.ran == []


def test_p3_with_approval_for_a_different_plan_hash_is_refused(env):
    h = env.plans.save(P3_PLAN)
    adv_h = env.advisors.save(advisor_record(h))
    other = plan_hash({**P3_PLAN, "target": "host-b"})
    env.approve(other, adv_h)
    with pytest.raises(GateRefused):
        env.run(h)
    assert env.ran == []


def test_advisor_record_for_a_different_plan_does_not_count(env):
    h = env.plans.save(P3_PLAN)
    other = plan_hash({**P3_PLAN, "target": "host-b"})
    adv_h = env.advisors.save(advisor_record(other))
    env.approve(h, adv_h)
    with pytest.raises(GateRefused, match="no advisor record"):
        env.run(h)


def test_advisor_on_non_advisor_pool_is_invalid(env):
    h = env.plans.save(P3_PLAN)
    env.advisors.save(advisor_record(h, pool="reasoning"))
    with pytest.raises(GateRefused, match="invalid"):
        env.run(h)


def test_full_p3_path_runs_after_abort_window_and_approval_is_single_use(env):
    h = env.plans.save(P3_PLAN)
    adv_h = env.advisors.save(advisor_record(h))
    env.approve(h, adv_h)
    assert env.run(h) == [0]
    assert env.mono >= 30  # the abort window elapsed before the step ran
    assert env.ran == ["reboot"]
    with pytest.raises(GateRefused):  # same approval cannot be used twice
        env.run(h)
    assert env.ran == ["reboot"]
    env.audit.verify()


def test_abort_within_window_cancels(env):
    h = env.plans.save(P3_PLAN)
    adv_h = env.advisors.save(advisor_record(h))
    env.approve(h, adv_h)
    with pytest.raises(GateAborted):
        env.run(h, aborted=lambda: env.mono >= 10)
    assert env.ran == []
    assert any(r["event"] == "gate.aborted" for r in env.audit.records())


def test_expired_approval_is_refused(env):
    h = env.plans.save(P3_PLAN)
    adv_h = env.advisors.save(advisor_record(h))
    env.approve(h, adv_h, ttl=300)
    env.now += 301
    with pytest.raises(GateRefused, match="expired"):
        env.run(h)
    assert env.ran == []


def test_tampered_stored_plan_is_refused(env, tmp_path):
    h = env.plans.save(P1_PLAN)
    path = tmp_path / "plans" / f"{h}.json"
    path.write_bytes(canonical({**P1_PLAN, "steps": [{"cmd": "reboot"}]}))
    with pytest.raises(GateRefused, match="does not match"):
        env.run(h)
    assert env.ran == []


def test_a_plan_that_is_really_p3_cannot_pass_as_lower_tier(env):
    # the tier is recomputed from the steps, whatever the plan claims about itself
    plan = {**P3_PLAN, "tier": "P1"}
    h = env.plans.save(plan)
    with pytest.raises(GateRefused):
        env.run(h)


def test_every_refusal_is_audited(env):
    h = env.plans.save(P3_PLAN)
    with pytest.raises(GateRefused):
        env.run(h)
    assert env.audit.records()[-1]["event"] == "gate.refused"


# --- advisor record schema (B-47) ---


def test_advisor_record_cannot_carry_an_approval():
    rec = advisor_record("a" * 64, approved=True)
    with pytest.raises(AdvisorRecordError, match="unknown"):
        validate_record(rec)


def test_advisor_record_requires_every_field():
    rec = advisor_record("a" * 64)
    del rec["verdict"]
    with pytest.raises(AdvisorRecordError, match="missing"):
        validate_record(rec)


def test_advisor_scores_are_range_checked():
    with pytest.raises(AdvisorRecordError, match="range"):
        validate_record(advisor_record("a" * 64, legitimacy_pct=101))


# --- audit chain ---


def test_audit_chain_detects_edit_and_deletion(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(3):
        log.append("e", {"i": i})
    assert log.verify() == 3
    lines = log.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["data"]["i"] = 99
    edited = [lines[0], json.dumps(rec, sort_keys=True, separators=(",", ":")), lines[2]]
    log.path.write_text("\n".join(edited) + "\n")
    with pytest.raises(AuditError):
        log.verify()
    log.path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    with pytest.raises(AuditError):
        log.verify()


def test_store_refuses_overwrite_with_different_content(tmp_path):
    s = PlanStore(tmp_path)
    h = s.save(P1_PLAN)
    (tmp_path / f"{h}.json").write_bytes(b"{}")
    with pytest.raises(StoreError):
        s.save(P1_PLAN)


def test_hashing_rejects_floats():
    with pytest.raises(TypeError):
        plan_hash({"x": 1.5})
