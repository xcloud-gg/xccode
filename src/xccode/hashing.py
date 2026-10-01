"""Canonical serialisation and hashes that bind plans, advisor records, and approvals together."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8. Floats are refused (hash drift)."""
    _reject_floats(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _reject_floats(obj: Any) -> None:
    if isinstance(obj, float):
        raise TypeError("floats are not allowed in hashed documents; use integers or strings")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError("hashed documents need string keys")
            _reject_floats(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _reject_floats(v)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def doc_hash(obj: Any) -> str:
    return sha256_hex(canonical(obj))


def plan_hash(plan: dict) -> str:
    """Hash of the whole plan; any change to any step, target, or reason changes it."""
    return doc_hash(plan)


def advisor_record_hash(record: dict) -> str:
    return doc_hash(record)


def short(h: str) -> str:
    """First 8 hex digits, the prefix the operator types to confirm."""
    return h[:8]
