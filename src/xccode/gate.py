"""The P3 executor gate: code, not policy (B-45).

A P3 plan runs only when ALL hold:
  1. the stored plan hashes to the hash being executed and its tier really is P3 (recomputed here);
  2. an advisor record exists whose plan_hash equals that hash and validates;
  3. a single-use approval exists for exactly (plan hash, advisor-record hash) and has not expired;
  4. the 30 s abort window passes without `abort`.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .advisor import AdvisorRecordError, validate_record
from .audit import AuditLog
from .store import AdvisorStore, ApprovalStore, PlanStore, StoreError
from .tiers import Tier, classify_plan

ABORT_WINDOW_S = 30


class GateRefused(Exception):
    pass


class GateAborted(GateRefused):
    pass


def execute_plan(
    plan_h: str,
    *,
    plans: PlanStore,
    advisors: AdvisorStore,
    approvals: ApprovalStore,
    audit: AuditLog,
    runner: Callable[[dict], int],
    aborted: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    abort_window: float = ABORT_WINDOW_S,
) -> list[int]:
    """Run a stored plan. P0-P2 plans need no gate here; P3 plans need advisor + approval."""
    try:
        plan = plans.load(plan_h)
    except StoreError as e:
        raise _refuse(audit, plan_h, f"plan: {e}") from e
    tier = classify_plan(plan.get("steps", [])).tier
    if tier == Tier.P3:
        _check_p3(plan_h, advisors, approvals, audit, aborted, sleep, clock, abort_window)
    audit.append("execute.start", {"plan": plan_h, "tier": tier.name})
    codes: list[int] = []
    for step in plan.get("steps", []):
        rc = runner(step)
        codes.append(rc)
        audit.append("execute.step", {"plan": plan_h, "cmd": step["cmd"], "rc": rc})
        if rc != 0:
            break
    audit.append("execute.end", {"plan": plan_h, "rcs": codes})
    return codes


def _refuse(audit: AuditLog, plan_h: str, why: str) -> GateRefused:
    audit.append("gate.refused", {"plan": plan_h, "why": why})
    return GateRefused(why)


def _check_p3(plan_h, advisors, approvals, audit, aborted, sleep, clock, abort_window) -> None:
    found = advisors.find_for_plan(plan_h)
    if found is None:
        raise _refuse(audit, plan_h, "P3 plan has no advisor record")
    adv_h, rec = found
    try:
        validate_record(rec, expected_plan_hash=plan_h)
    except AdvisorRecordError as e:
        raise _refuse(audit, plan_h, f"advisor record invalid: {e}") from e
    try:
        approvals.consume(plan_h, adv_h)
    except StoreError as e:
        raise _refuse(audit, plan_h, f"no valid approval: {e}") from e
    audit.append(
        "gate.approved", {"plan": plan_h, "advisor": adv_h, "abort_window_s": abort_window}
    )
    deadline = clock() + abort_window
    while clock() < deadline:
        if aborted():
            audit.append("gate.aborted", {"plan": plan_h})
            raise GateAborted("aborted by the operator during the abort window")
        sleep(min(0.5, max(0.0, deadline - clock())))
    if aborted():
        audit.append("gate.aborted", {"plan": plan_h})
        raise GateAborted("aborted by the operator during the abort window")
