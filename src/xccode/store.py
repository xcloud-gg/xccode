"""Content-addressed stores for plans, advisor records, and approvals.

Files are named by the hash of their content and are never overwritten with different content.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .hashing import advisor_record_hash, canonical, plan_hash


class StoreError(Exception):
    pass


def _write_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise StoreError(f"{path.name} exists with different content")
        return
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


class PlanStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def save(self, plan: dict) -> str:
        h = plan_hash(plan)
        _write_once(self.root / f"{h}.json", canonical(plan))
        return h

    def load(self, h: str) -> dict:
        path = self.root / f"{h}.json"
        if not path.exists():
            raise StoreError(f"no stored plan {h[:8]}")
        plan = json.loads(path.read_text())
        if plan_hash(plan) != h:
            raise StoreError(f"stored plan {h[:8]} does not match its hash")
        return plan


class AdvisorStore:
    """Advisor records keyed by their own hash; looked up by the plan hash they cover."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def save(self, record: dict) -> str:
        h = advisor_record_hash(record)
        _write_once(self.root / f"{h}.json", canonical(record))
        return h

    def load(self, h: str) -> dict:
        path = self.root / f"{h}.json"
        if not path.exists():
            raise StoreError(f"no advisor record {h[:8]}")
        rec = json.loads(path.read_text())
        if advisor_record_hash(rec) != h:
            raise StoreError(f"advisor record {h[:8]} does not match its hash")
        return rec

    def find_for_plan(self, plan_h: str) -> tuple[str, dict] | None:
        if not self.root.exists():
            return None
        found: tuple[str, dict] | None = None
        for p in sorted(self.root.glob("*.json")):
            rec = self.load(p.stem)
            if rec.get("plan_hash") == plan_h:
                found = (p.stem, rec)  # any matching record will do
        return found


@dataclass(frozen=True)
class Approval:
    pending_id: str
    plan_hash: str
    advisor_hash: str
    approved_at: int
    expires_at: int

    def to_dict(self) -> dict:
        return {
            "pending_id": self.pending_id,
            "plan_hash": self.plan_hash,
            "advisor_hash": self.advisor_hash,
            "approved_at": self.approved_at,
            "expires_at": self.expires_at,
        }


class ApprovalStore:
    """Single-use approvals. `consume` deletes the file, so an approval works once."""

    def __init__(self, root: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self._clock = clock

    def _path(self, plan_h: str, adv_h: str) -> Path:
        return self.root / f"{plan_h}.{adv_h}.json"

    def grant(self, a: Approval) -> None:
        _write_once(self._path(a.plan_hash, a.advisor_hash), canonical(a.to_dict()))

    def consume(self, plan_h: str, adv_h: str) -> Approval:
        path = self._path(plan_h, adv_h)
        try:
            os.rename(path, path.with_suffix(".used"))  # atomic: only one caller wins
        except FileNotFoundError as e:
            raise StoreError("no approval for this plan and advisor record") from e
        data = json.loads(path.with_suffix(".used").read_text())
        a = Approval(**data)
        if a.plan_hash != plan_h or a.advisor_hash != adv_h:
            raise StoreError("approval does not match")
        if self._clock() > a.expires_at:
            raise StoreError("approval expired")
        return a

    def clear(self) -> int:
        n = 0
        if self.root.exists():
            for p in self.root.glob("*.json"):
                p.unlink()
                n += 1
        return n
