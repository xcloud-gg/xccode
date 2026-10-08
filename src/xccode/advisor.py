"""Advisor record schema and the interface xcroute implements.

The advisor informs; it cannot approve. The record has no approval field and unknown fields are
refused, so no code path can turn an advisor answer into an approval (B-47). The advisor runs on the
reserved frontier model (`claude-opus-5-5` on the direct Anthropic key), recorded as the `advisor`
pool (§15.6).
"""

from __future__ import annotations

from typing import Protocol

REQUIRED_POOL = "advisor"

_FIELDS = {
    "plan_hash": str,
    "pool": str,
    "origin": dict,  # agent, session, hand-off or pull-request reference
    "legitimacy_pct": int,  # advisor estimate 0-100, shown beside the evidence, never alone
    "quality_score": int,  # 0-10
    "evidence": dict,  # {"audit_log": bool, "signed_handoff": bool, "task_matches": bool}
    "blast_radius": str,
    "rollback": str,
    "verdict": str,
    "sent_payload_hash": str,  # hash of exactly what left the host (B-51)
}
_EVIDENCE_KEYS = {"audit_log", "signed_handoff", "task_matches"}


class AdvisorRecordError(ValueError):
    pass


def validate_record(rec: dict, expected_plan_hash: str | None = None) -> dict:
    extra = set(rec) - set(_FIELDS)
    if extra:
        raise AdvisorRecordError(f"unknown advisor fields: {sorted(extra)}")
    for name, typ in _FIELDS.items():
        if name not in rec:
            raise AdvisorRecordError(f"missing advisor field: {name}")
        if not isinstance(rec[name], typ) or isinstance(rec[name], bool) and typ is int:
            raise AdvisorRecordError(f"advisor field {name} must be {typ.__name__}")
    if rec["pool"] != REQUIRED_POOL:
        raise AdvisorRecordError(f"advisor must run on {REQUIRED_POOL}, not {rec['pool']!r}")
    if not 0 <= rec["legitimacy_pct"] <= 100 or not 0 <= rec["quality_score"] <= 10:
        raise AdvisorRecordError("score out of range")
    if set(rec["evidence"]) != _EVIDENCE_KEYS or not all(
        isinstance(v, bool) for v in rec["evidence"].values()
    ):
        raise AdvisorRecordError("evidence needs exactly audit_log, signed_handoff, task_matches")
    if expected_plan_hash is not None and rec["plan_hash"] != expected_plan_hash:
        raise AdvisorRecordError("advisor record covers a different plan")
    return rec


class Advisor(Protocol):
    def review(self, plan: dict, plan_hash: str) -> dict:
        """Return a record that passes `validate_record`. Read-only; must not act."""
