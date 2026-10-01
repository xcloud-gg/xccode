"""Append-only, hash-chained audit log (JSON lines).

Every record carries the hash of the previous one, so a deleted or edited line breaks `verify()`.
The installer also sets the append-only attribute on the file; this module never rewrites it.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .hashing import doc_hash

GENESIS = "0" * 64


class AuditError(Exception):
    pass


class AuditLog:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _last(self, fd: int) -> tuple[int, str]:
        with os.fdopen(os.dup(fd), "rb") as f:
            f.seek(0)
            seq, prev = 0, GENESIS
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    seq, prev = rec["seq"], rec["hash"]
            return seq, prev

    def append(self, event: str, data: dict[str, Any] | None = None) -> dict:
        fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            seq, prev = self._last(fd)
            body = {
                "seq": seq + 1,
                "ts": int(self._clock()),
                "event": event,
                "data": data or {},
                "prev": prev,
            }
            rec = {**body, "hash": doc_hash(body)}
            os.write(fd, json.dumps(rec, sort_keys=True, separators=(",", ":")).encode() + b"\n")
            os.fsync(fd)
            return rec
        finally:
            os.close(fd)

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]

    def verify(self) -> int:
        """Return the record count, or raise AuditError at the first broken link."""
        prev, n = GENESIS, 0
        for rec in self.records():
            body = {k: rec[k] for k in ("seq", "ts", "event", "data", "prev")}
            if rec["prev"] != prev or rec["hash"] != doc_hash(body) or rec["seq"] != n + 1:
                raise AuditError(f"audit chain broken at seq {rec.get('seq')}")
            prev, n = rec["hash"], n + 1
        return n
