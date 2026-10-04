"""xccode-collect — turn finished OpenCode sessions into learning digests (XC-CODE-001 §6.1).

Collect is the first stage of the learning pipeline (collect → /learn → digests → pre-check →
Hermes). It runs as ``marius`` (a user unit, at session end and hourly), so it writes nothing into
xccode's root-owned state tree: it redacts everything with Guard-lite and posts the digests to
xcroute's ``/learn`` door (which runs as ``xccode`` and owns the spool). A local spool under
``~/.local/share/xccode/collect`` keeps the exact redacted payload for the operator to audit, so a
failed ``/learn`` post is never silent and never loses work.

A digest is one finished unit of work: an OpenCode session, a plan/review note from
``~/.local/share/xccode/thoughts/<repo>/``, a dsh job log, or a correction the operator typed.
Guard-lite (``xcroute.guard.redact``) runs over every field before anything leaves the host.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .hashing import canonical, sha256_hex
from .xcroute.guard import redact

XCROUTE = os.environ.get("XCC_ROUTE", "http://127.0.0.1:18080")
THOUGHTS_DIR = Path.home() / ".local/share/xccode/thoughts"
SPOOL_DIR = Path.home() / ".local/share/xccode/collect"


@dataclass(frozen=True)
class Digest:
    """One unit of finished work, ready for the learner (after Guard-lite)."""

    source: str  # opencode-session | thoughts | dsh-job | operator-correction
    repo: str
    task: str
    files_touched: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    outcome: str = ""
    notes: str = ""  # OAC plan/review notes or the operator's correction text


@dataclass
class Collected:
    """A digest plus the Guard-lite hit report, so the operator sees what was redacted."""

    digest: dict[str, Any]
    hits: tuple[str, ...] = ()


def _redact_text(text: str) -> tuple[str, tuple[str, ...]]:
    r = redact(text)
    return r.text, r.hits


def redact_digest(digest: Digest) -> Collected:
    """Run Guard-lite over every free-text field; return the redacted digest and the rule hits."""
    hits: list[str] = []
    out: dict[str, Any] = {
        "source": digest.source,
        "repo": digest.repo,
        "task": "",
        "files_touched": [],
        "commands": [],
        "outcome": "",
        "notes": "",
    }
    for field in ("task", "outcome", "notes"):
        text, h = _redact_text(getattr(digest, field) or "")
        out[field] = text
        hits += [h for h in h if h not in hits]
    for field in ("files_touched", "commands"):
        clean: list[str] = []
        for item in getattr(digest, field):
            text, h = _redact_text(item)
            clean.append(text)
            hits += [h for h in h if h not in hits]
        out[field] = clean
    return Collected(digest=out, hits=tuple(hits))


def digest_id(digest: dict[str, Any]) -> str:
    """Stable id for a digest, from its canonical (redacted) content."""
    return sha256_hex(canonical(digest))


def to_document(digest: dict[str, Any], collected_at: str | None = None) -> dict[str, Any]:
    """Wrap a redacted digest in the learner document with its id and timestamp."""
    doc = {
        "id": digest_id(digest),
        "collected_at": collected_at or datetime.now(UTC).isoformat(),
        **digest,
    }
    return doc


def read_sessions(sessions_dir: Path) -> list[Digest]:
    """Best-effort scan of an OpenCode session directory into digests.

    OpenCode v2 keeps each session as a JSON object with a ``title`` and a ``messages`` list. The
    scanner is deliberately tolerant: it reads every ``*.json`` that is not obviously a list, takes
    ``title`` (or ``directory``) as the task, and derives the outcome from the last assistant
    message. Files that do not parse are skipped — collect must never fail the timer.
    """
    if not sessions_dir.is_dir():
        return []
    digests: list[Digest] = []
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        task = str(data.get("title") or data.get("directory") or path.stem)
        files = tuple(
            sorted({str(m.get("file", "")) for m in data.get("files", []) if m.get("file")})
        )
        outcome = _last_assistant(data.get("messages", []))
        digests.append(
            Digest(
                source="opencode-session",
                repo=task,
                task=task,
                files_touched=files,
                outcome=outcome,
            )
        )
    return digests


def _last_assistant(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for m in reversed(messages):
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            content = m.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [
                    p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")
                ]
                if parts:
                    return "".join(parts)
    return ""


def read_thoughts(thoughts_dir: Path) -> list[Digest]:
    """Read OAC plan/review notes from ``~/.local/share/xccode/thoughts/<repo>/``.

    Each note is a markdown file under a per-repo directory; the file name is the note kind
    (``plan``/``review``/…) and its body is the note text.
    """
    if not thoughts_dir.is_dir():
        return []
    digests: list[Digest] = []
    for repo_dir in sorted(thoughts_dir.iterdir()):
        if not repo_dir.is_dir():
            continue
        for note in sorted(repo_dir.glob("*.md")):
            try:
                body = note.read_text()
            except OSError:
                continue
            digests.append(
                Digest(
                    source="thoughts",
                    repo=repo_dir.name,
                    task=note.stem,
                    notes=body,
                )
            )
    return digests


def collect(
    sessions_dir: Path, thoughts_dir: Path, corrections: list[Digest] | None = None
) -> list[Collected]:
    """Scan every source, run Guard-lite over each digest, and return the redacted set."""
    raw = read_sessions(sessions_dir) + read_thoughts(thoughts_dir) + list(corrections or [])
    return [redact_digest(d) for d in raw]


def post_digest(doc: dict[str, Any], token: str) -> bool:
    """Post one redacted digest to xcroute's ``/learn`` door. Returns True on 2xx."""
    r = httpx.post(
        f"{XCROUTE}/learn",
        json=doc,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    return r.status_code < 300


def spool(doc: dict[str, Any], spool_dir: Path = SPOOL_DIR) -> Path:
    """Persist the exact redacted payload locally before it is posted (audit + retry)."""
    spool_dir.mkdir(parents=True, exist_ok=True)
    path = spool_dir / f"{doc['id']}.json"
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, canonical(doc))
        finally:
            os.close(fd)
        os.replace(tmp, path)
    return path


def run(
    sessions_dir: Path,
    thoughts_dir: Path,
    token: str,
    spool_dir: Path = SPOOL_DIR,
    corrections: list[Digest] | None = None,
) -> tuple[int, int]:
    """Full collect pass: scan → redact → spool → post. Returns (digests, posted)."""
    collected = collect(sessions_dir, thoughts_dir, corrections)
    posted = 0
    for c in collected:
        doc = to_document(c.digest)
        spool(doc, spool_dir)
        if token and post_digest(doc, token):
            posted += 1
    return len(collected), posted
