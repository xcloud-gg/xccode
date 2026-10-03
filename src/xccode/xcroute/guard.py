"""Guard-lite: outbound secret redaction (§4.6). Every outgoing message passes through here."""

from __future__ import annotations

import re
from dataclasses import dataclass

# (rule name, pattern). Names are logged and shown to the operator; matched text never is.
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(
        r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----.*?(?:-----END (?:[A-Z]+ )?PRIVATE KEY-----|\Z)",
        re.DOTALL)),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-style-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{24,}")),
    ("assigned-secret", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|passw(?:or)?d|token)\b\s*[:=]\s*['\"]?[^\s'\"]{12,}")),
)


@dataclass(frozen=True)
class Redaction:
    text: str
    hits: tuple[str, ...]  # rule names, in order of first occurrence, no duplicates


def redact(text: str) -> Redaction:
    hits: list[str] = []
    for name, pattern in RULES:
        text, n = pattern.subn(f"[REDACTED:{name}]", text)
        if n and name not in hits:
            hits.append(name)
    return Redaction(text, tuple(hits))


def redact_messages(messages: list[dict]) -> tuple[list[dict], tuple[str, ...]]:
    """Redact every string content of an OpenAI-style message list; returns copies."""
    out: list[dict] = []
    hits: list[str] = []
    for m in messages:
        m = dict(m)
        content = m.get("content")
        if isinstance(content, str):
            r = redact(content)
            m["content"] = r.text
            hits += [h for h in r.hits if h not in hits]
        elif isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    r = redact(p["text"])
                    p = {**p, "text": r.text}
                    hits += [h for h in r.hits if h not in hits]
                parts.append(p)
            m["content"] = parts
        out.append(m)
    return out, tuple(hits)
