"""The operator's approver for P3 plans (XC-DES-001 §25.5, B-46).

What it enforces, in code:
  * the password is verified against an argon2id hash that only the `xccode` account can read;
    the password itself is never stored, logged, or put in an argument or environment variable;
  * one P3 request is pending at a time and it expires after 5 minutes;
  * the operator confirms by typing the pending id and the first 8 hex digits of the plan hash and
    of the advisor-record hash BEFORE the password is asked; a changed plan is a different hash and
    therefore a different, unapproved request;
  * three wrong passwords lock the approver for 15 minutes and raise an alert;
  * prompting is refused inside tmux, screen, or an X11 terminal unless `allow_unsafe_tty` is given
    (and then it is logged as an exception);
  * after approval the executor waits 30 s during which `abort` cancels it;
  * `reset` works only as root, removes the hash, and voids pending requests and approvals.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .audit import AuditLog
from .hashing import short
from .store import AdvisorStore, Approval, ApprovalStore, PlanStore

PENDING_TTL_S = 300
APPROVAL_TTL_S = 300
MAX_FAILS = 3
LOCKOUT_S = 15 * 60
MIN_PASSWORD_CHARS = 16
MIN_PASSPHRASE_WORDS = 5


class ApproverError(Exception):
    pass


class Locked(ApproverError):
    pass


@dataclass(frozen=True)
class Pending:
    pending_id: str
    plan_hash: str
    advisor_hash: str
    created_at: int
    expires_at: int


def password_acceptable(pw: str) -> str | None:
    """Return a reason string if the password is too weak, else None."""
    words = [w for w in re.split(r"\s+", pw.strip()) if w]
    if len(pw) >= MIN_PASSWORD_CHARS or len(words) >= MIN_PASSPHRASE_WORDS:
        return None
    return (
        f"use at least {MIN_PASSWORD_CHARS} characters or a passphrase of "
        f"{MIN_PASSPHRASE_WORDS} words"
    )


def check_tty_safe(
    environ: Mapping[str, str], tty_path: str | None, allow_unsafe: bool = False
) -> str | None:
    """Return None when it is safe to prompt, else the reason it is not.

    Unsafe: tmux or screen (anything running as the same user can inject keystrokes through the
    multiplexer), and X11 terminals (any X client can read keystrokes). A text console or an SSH
    session from another device is the supported place. Returns the string "exception" instead of
    None when the caller overrode the check, so it can be audited.
    """
    problems = []
    term = environ.get("TERM", "")
    if "TMUX" in environ or "STY" in environ or term.startswith(("screen", "tmux")):
        problems.append("a terminal multiplexer (tmux or screen)")
    over_ssh = "SSH_CONNECTION" in environ or "SSH_TTY" in environ
    if (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")) and not over_ssh:
        problems.append("a graphical session (X11 or Wayland terminal)")
    if tty_path is None:
        problems.append("no controlling terminal")
    if not problems:
        return None
    if allow_unsafe and tty_path is not None:
        return "exception"
    return "refusing to prompt inside " + " and ".join(problems)


class Approver:
    def __init__(
        self,
        *,
        etc_dir: Path | str,
        state_dir: Path | str,
        audit: AuditLog,
        plans: PlanStore,
        advisors: AdvisorStore,
        approvals: ApprovalStore,
        clock: Callable[[], float] = time.time,
        is_root: Callable[[], bool] = lambda: os.geteuid() == 0,
        alert: Callable[[str, dict], None] = lambda event, data: None,
    ) -> None:
        self.hash_path = Path(etc_dir) / "approver.hash"
        self.state_path = Path(state_dir) / "approver-state.json"
        self.pending_path = Path(state_dir) / "pending.json"
        self.abort_path = Path(state_dir) / "abort"
        self.audit, self.plans, self.advisors, self.approvals = audit, plans, advisors, approvals
        self._clock, self._is_root, self._alert = clock, is_root, alert
        self._ph = PasswordHasher()  # argon2id, random per-hash salt
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    # --- enrolment and reset -------------------------------------------------------------------
    def enroll(self, password: str) -> None:
        if self.hash_path.exists():
            raise ApproverError("already enrolled; a root `reset` is required to change it")
        weak = password_acceptable(password)
        if weak:
            raise ApproverError(weak)
        self.hash_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.hash_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            os.write(fd, self._ph.hash(password).encode())
        finally:
            os.close(fd)
        self.audit.append("approver.enrolled", {})

    def reset(self) -> None:
        if not self._is_root():
            self.audit.append("approver.reset.refused", {"why": "not root"})
            raise ApproverError("reset works only as root")
        for p in (self.hash_path, self.pending_path, self.state_path, self.abort_path):
            p.unlink(missing_ok=True)
        voided = self.approvals.clear()
        self.audit.append("approver.reset", {"approvals_voided": voided})
        self._alert("approver.reset", {})

    @property
    def enrolled(self) -> bool:
        return self.hash_path.exists()

    # --- throttle ------------------------------------------------------------------------------
    def _state(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"fails": 0, "locked_until": 0}

    def _save_state(self, st: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        os.replace(tmp, self.state_path)

    def locked_for(self) -> int:
        remaining = int(self._state()["locked_until"] - self._clock())
        return max(0, remaining)

    # --- requests ------------------------------------------------------------------------------
    def current_pending(self) -> Pending | None:
        if not self.pending_path.exists():
            return None
        p = Pending(**json.loads(self.pending_path.read_text()))
        if self._clock() > p.expires_at:
            self.pending_path.unlink(missing_ok=True)
            self.audit.append("approver.pending.expired", {"id": p.pending_id})
            return None
        return p

    def request(self, plan_h: str, advisor_h: str) -> Pending:
        """Open a pending P3 approval for a stored plan that has a matching advisor record."""
        self.plans.load(plan_h)
        rec = self.advisors.load(advisor_h)
        if rec.get("plan_hash") != plan_h:
            raise ApproverError("the advisor record covers a different plan")
        if self.current_pending() is not None:
            raise ApproverError("another P3 approval is already pending")
        now = int(self._clock())
        p = Pending(secrets.token_hex(4), plan_h, advisor_h, now, now + PENDING_TTL_S)
        tmp = self.pending_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(p.__dict__))
        os.replace(tmp, self.pending_path)
        self.audit.append(
            "approver.requested", {"id": p.pending_id, "plan": plan_h, "advisor": advisor_h}
        )
        self._alert("approver.requested", {"id": p.pending_id, "plan": short(plan_h)})
        return p

    def confirmation_required(self, p: Pending) -> tuple[str, str, str]:
        """What the operator must type: id, plan-hash prefix, advisor-hash prefix."""
        return p.pending_id, short(p.plan_hash), short(p.advisor_hash)

    # --- approval ------------------------------------------------------------------------------
    def check_confirmation(self, p: Pending, typed: tuple[str, str, str]) -> bool:
        ok = tuple(t.strip().lower() for t in typed) == self.confirmation_required(p)
        if not ok:
            self.audit.append("approver.confirm.mismatch", {"id": p.pending_id})
        return ok

    def verify_and_grant(
        self, p: Pending, password: str, *, tty_exception: bool = False
    ) -> Approval:
        """Verify the password for pending request `p` and grant a single-use approval."""
        if not self.enrolled:
            raise ApproverError("no approver password is enrolled (`xccode approve enroll`)")
        left = self.locked_for()
        if left:
            self.audit.append("approver.locked.attempt", {"id": p.pending_id, "seconds_left": left})
            raise Locked(f"locked for another {left} s")
        live = self.current_pending()
        if live is None or live.pending_id != p.pending_id:
            raise ApproverError("this request is no longer pending")
        try:
            self._ph.verify(self.hash_path.read_text().strip(), password)
        except (VerificationError, InvalidHashError):
            return self._fail(p)
        st = {"fails": 0, "locked_until": 0}
        self._save_state(st)
        now = int(self._clock())
        approval = Approval(p.pending_id, p.plan_hash, p.advisor_hash, now, now + APPROVAL_TTL_S)
        self.approvals.grant(approval)
        self.pending_path.unlink(missing_ok=True)
        self.abort_path.unlink(missing_ok=True)
        self.audit.append(
            "approver.approved",
            {"id": p.pending_id, "plan": p.plan_hash, "advisor": p.advisor_hash,
             "tty_exception": tty_exception},
        )
        self._alert("approver.approved", {"id": p.pending_id, "plan": short(p.plan_hash)})
        return approval

    def _fail(self, p: Pending) -> Approval:
        st = self._state()
        st["fails"] += 1
        locked = st["fails"] >= MAX_FAILS
        if locked:
            st["locked_until"] = int(self._clock()) + LOCKOUT_S
            st["fails"] = 0
        self._save_state(st)
        self.audit.append("approver.denied", {"id": p.pending_id, "locked": locked})
        self._alert("approver.denied", {"id": p.pending_id, "locked": locked})
        if locked:
            raise Locked(f"three wrong passwords: locked for {LOCKOUT_S // 60} minutes")
        raise ApproverError("wrong password")

    # --- abort ---------------------------------------------------------------------------------
    def abort(self) -> None:
        self.abort_path.write_text(str(int(self._clock())))
        self.audit.append("approver.abort", {})

    def aborted(self) -> bool:
        return self.abort_path.exists()

    def clear_abort(self) -> None:
        self.abort_path.unlink(missing_ok=True)
