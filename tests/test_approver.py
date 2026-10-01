import pytest
from argon2 import PasswordHasher

from tests.test_gate import P3_PLAN, advisor_record
from xccode.approver import (
    LOCKOUT_S,
    PENDING_TTL_S,
    Approver,
    ApproverError,
    Locked,
    check_tty_safe,
    password_acceptable,
)
from xccode.audit import AuditLog
from xccode.gate import GateAborted, GateRefused, execute_plan
from xccode.store import AdvisorStore, ApprovalStore, PlanStore

PW = "correct horse battery staple"


class Rig:
    def __init__(self, tmp_path, is_root=False):
        self.now = 2_000_000.0
        self.audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: self.now)
        self.plans = PlanStore(tmp_path / "plans")
        self.advisors = AdvisorStore(tmp_path / "advisor")
        self.approvals = ApprovalStore(tmp_path / "approvals", clock=lambda: self.now)
        self.alerts = []
        self.ap = Approver(
            etc_dir=tmp_path / "etc",
            state_dir=tmp_path / "state",
            audit=self.audit,
            plans=self.plans,
            advisors=self.advisors,
            approvals=self.approvals,
            clock=lambda: self.now,
            is_root=lambda: is_root,
            alert=lambda e, d: self.alerts.append(e),
        )
        self.ap._ph = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
        self.plan_h = self.plans.save(P3_PLAN)
        self.adv_h = self.advisors.save(advisor_record(self.plan_h))

    def pending(self):
        return self.ap.request(self.plan_h, self.adv_h)


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.ap.enroll(PW)
    return r


def test_weak_passwords_are_refused(tmp_path):
    r = Rig(tmp_path)
    assert password_acceptable("short") is not None
    assert password_acceptable("sixteen chars ok!") is None
    assert password_acceptable("a b c d e") is None  # five words
    with pytest.raises(ApproverError):
        r.ap.enroll("short")


def test_only_a_salted_argon2id_hash_is_stored(rig, tmp_path):
    data = (tmp_path / "etc" / "approver.hash").read_text()
    assert data.startswith("$argon2id$")
    assert PW not in data
    assert (tmp_path / "etc" / "approver.hash").stat().st_mode & 0o777 == 0o640
    for rec in rig.audit.records():
        assert PW not in str(rec)


def test_enroll_twice_requires_reset(rig):
    with pytest.raises(ApproverError, match="reset"):
        rig.ap.enroll("another long password here")


def test_confirmation_must_match_id_and_both_hash_prefixes(rig):
    p = rig.pending()
    pid, a, b = rig.ap.confirmation_required(p)
    assert rig.ap.check_confirmation(p, (pid, a, b))
    assert rig.ap.check_confirmation(p, (pid.upper(), a, b))
    assert not rig.ap.check_confirmation(p, (pid, a, "0" * 8))
    assert not rig.ap.check_confirmation(p, ("deadbeef", a, b))


def test_correct_password_grants_a_single_use_approval_that_the_gate_accepts(rig):
    ran = []
    p = rig.pending()
    rig.ap.verify_and_grant(p, PW)
    mono = [0.0]
    kw = dict(
        plans=rig.plans, advisors=rig.advisors, approvals=rig.approvals, audit=rig.audit,
        runner=lambda s: (ran.append(s["cmd"]), 0)[1],
        sleep=lambda s: mono.__setitem__(0, mono[0] + s), clock=lambda: mono[0],
        aborted=rig.ap.aborted,
    )
    assert execute_plan(rig.plan_h, **kw) == [0]
    assert ran == ["reboot"]
    with pytest.raises(GateRefused):
        execute_plan(rig.plan_h, **kw)
    assert "approver.approved" in [r["event"] for r in rig.audit.records()]
    rig.audit.verify()


def test_abort_in_the_window_stops_the_executor(rig):
    p = rig.pending()
    rig.ap.verify_and_grant(p, PW)
    mono = [0.0]

    def sleep(s):
        mono[0] += s
        if mono[0] > 5:
            rig.ap.abort()

    with pytest.raises(GateAborted):
        execute_plan(
            rig.plan_h, plans=rig.plans, advisors=rig.advisors, approvals=rig.approvals,
            audit=rig.audit, runner=lambda s: 0, sleep=sleep, clock=lambda: mono[0],
            aborted=rig.ap.aborted,
        )


def test_three_wrong_passwords_lock_for_fifteen_minutes_and_alert(rig):
    p = rig.pending()
    for _ in range(2):
        with pytest.raises(ApproverError, match="wrong"):
            rig.ap.verify_and_grant(p, "nope nope nope nope")
    with pytest.raises(Locked):
        rig.ap.verify_and_grant(p, "nope nope nope nope")
    # even the right password is refused while locked
    with pytest.raises(Locked):
        rig.ap.verify_and_grant(p, PW)
    assert "approver.denied" in rig.alerts
    rig.now += LOCKOUT_S + 1
    p = rig.ap.request(rig.plan_h, rig.adv_h) if rig.ap.current_pending() is None else p
    assert rig.ap.verify_and_grant(p, PW)


def test_one_pending_at_a_time_and_expiry(rig):
    rig.pending()
    with pytest.raises(ApproverError, match="already pending"):
        rig.pending()
    rig.now += PENDING_TTL_S + 1
    assert rig.pending()  # the old one expired


def test_expired_request_cannot_be_approved(rig):
    p = rig.pending()
    rig.now += PENDING_TTL_S + 1
    with pytest.raises(ApproverError, match="no longer pending"):
        rig.ap.verify_and_grant(p, PW)


def test_advisor_record_for_another_plan_cannot_open_a_request(rig):
    other_plan = rig.plans.save({**P3_PLAN, "target": "host-z"})
    with pytest.raises(ApproverError, match="different plan"):
        rig.ap.request(other_plan, rig.adv_h)


def test_changed_plan_is_a_new_hash_and_the_old_approval_does_not_cover_it(rig):
    p = rig.pending()
    rig.ap.verify_and_grant(p, PW)
    changed = rig.plans.save({**P3_PLAN, "steps": [{"cmd": "reboot"}, {"cmd": "poweroff"}]})
    rig.advisors.save(advisor_record(changed))
    with pytest.raises(GateRefused):
        execute_plan(
            changed, plans=rig.plans, advisors=rig.advisors, approvals=rig.approvals,
            audit=rig.audit, runner=lambda s: 0, sleep=lambda s: None, clock=lambda: 0.0,
        )


def test_reset_needs_root_and_voids_everything(tmp_path):
    r = Rig(tmp_path, is_root=False)
    r.ap.enroll(PW)
    p = r.pending()
    r.ap.verify_and_grant(p, PW)
    with pytest.raises(ApproverError, match="root"):
        r.ap.reset()
    assert r.ap.enrolled

    root = Rig(tmp_path / "root", is_root=True)
    root.ap.enroll(PW)
    p = root.pending()
    root.ap.verify_and_grant(p, PW)
    root.pending()
    root.ap.reset()
    assert not root.ap.enrolled
    assert root.ap.current_pending() is None
    assert root.approvals.clear() == 0


# --- tty rules -----------------------------------------------------------------------------------


def test_prompt_refused_in_tmux_screen_and_graphical_sessions():
    assert check_tty_safe({"TMUX": "/tmp/x"}, "/dev/pts/3").startswith("refusing")
    assert check_tty_safe({"STY": "1"}, "/dev/pts/3").startswith("refusing")
    assert check_tty_safe({"TERM": "screen-256color"}, "/dev/pts/3").startswith("refusing")
    assert check_tty_safe({"DISPLAY": ":0"}, "/dev/pts/3").startswith("refusing")
    assert check_tty_safe({"WAYLAND_DISPLAY": "wayland-0"}, "/dev/pts/3").startswith("refusing")


def test_prompt_allowed_on_a_text_console_and_over_ssh():
    assert check_tty_safe({"TERM": "linux"}, "/dev/tty3") is None
    assert check_tty_safe({"TERM": "xterm", "SSH_CONNECTION": "a b c d"}, "/dev/pts/1") is None
    # ssh with X forwarding is still fine: the keystrokes travel from another device
    env = {"TERM": "xterm", "SSH_TTY": "/dev/pts/1", "DISPLAY": "localhost:10.0"}
    assert check_tty_safe(env, "/dev/pts/1") is None


def test_unsafe_override_is_reported_as_an_exception_and_needs_a_tty():
    assert check_tty_safe({"TMUX": "x"}, "/dev/pts/3", allow_unsafe=True) == "exception"
    assert "no controlling terminal" in check_tty_safe({}, None, allow_unsafe=True)
