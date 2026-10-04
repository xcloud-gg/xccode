"""verifier-lite: the gate at xcroute's ``/learn`` door (XC-CODE-001 §6.4).

A learning digest reaches the nightly learner only if it is well-formed, id-bound to its content,
secret-free, not already queued, and within a size bound. ``xccode-collect`` (as the operator) posts
Guard-lite-redacted digests here; verifier-lite is the second line that rejects anything malformed,
unprovenanced, secret-bearing, duplicated, or oversized. Valid digests are written to
``learn/pending/<id>.json`` for ``xccode-learn.sh`` to pick up.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..hashing import canonical, sha256_hex
from .guard import redact

# The fields a digest's id binds (everything except `id` and `collected_at`), matching the shape
# `xccode.collect.redact_digest` produces.
CONTENT_FIELDS = ("source", "repo", "task", "files_touched", "commands", "outcome", "notes")
MAX_DIGEST_BYTES = 65536


def digest_id(digest: dict[str, Any]) -> str:
    """The id a digest must carry: the hash of its content fields only."""
    content = {
        "source": digest.get("source", ""),
        "repo": digest.get("repo", ""),
        "task": digest.get("task", ""),
        "files_touched": digest.get("files_touched", []),
        "commands": digest.get("commands", []),
        "outcome": digest.get("outcome", ""),
        "notes": digest.get("notes", ""),
    }
    return sha256_hex(canonical(content))


def verify(digest: dict[str, Any], pending: set[str]) -> list[str]:
    """Return the problems (empty = valid). ``pending`` is the set of ids already queued."""
    problems: list[str] = []
    for field in ("source", "repo", "task"):
        value = digest.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"missing or empty {field}")
    for field in ("files_touched", "commands"):
        if field in digest and not isinstance(digest[field], list):
            problems.append(f"{field} is not a list")
    for field in ("outcome", "notes"):
        value = digest.get(field)
        if value is not None and not isinstance(value, str):
            problems.append(f"{field} is not a string")
    if not isinstance(digest.get("id"), str) or not digest["id"]:
        problems.append("missing id")
    elif digest["id"] != digest_id(digest):
        problems.append("id does not match content")
    for field in ("task", "outcome", "notes"):
        value = digest.get(field, "")
        if isinstance(value, str):
            hits = redact(value).hits
            if hits:
                problems.append(f"secret in {field} ({hits[0]})")
    if digest.get("id") in pending:
        problems.append("duplicate")
    if len(canonical(digest)) > MAX_DIGEST_BYTES:
        problems.append("oversized")
    return problems


def intake(pending_dir: Path, digest: dict[str, Any]) -> tuple[bool, list[str]]:
    """Verify, then write the digest to ``pending/<id>.json``; returns (queued, problems)."""
    pending_dir.mkdir(parents=True, exist_ok=True)
    pending = {p.stem for p in pending_dir.glob("*.json")}
    problems = verify(digest, pending)
    if problems:
        return False, problems
    path = pending_dir / f"{digest['id']}.json"
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        try:
            os.write(fd, canonical(digest))
        finally:
            os.close(fd)
        os.replace(tmp, path)
    return True, []
