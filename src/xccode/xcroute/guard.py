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
    ("age-secret-key", re.compile(r"\bAGE-SECRET-KEY-1[A-Za-z0-9]{40,}\b")),
    ("vault-token", re.compile(r"\bhvs\.[A-Za-z0-9_-]{24,}\b")),
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
    """Redact every string content of an OpenAI-style message list; returns copies.

    Covers the whole tool loop (§7): plain text content, content parts, and
    ``tool_calls[].function.arguments`` — a `write_file` of a `.env` is exactly where a key
    would otherwise slip past (advisor C2/O4)."""
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
        for tc in m.get("tool_calls") or ():
            if isinstance(tc, dict):
                fn = tc.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                    r = redact(fn["arguments"])
                    fn["arguments"] = r.text
                    hits += [h for h in r.hits if h not in hits]
        out.append(m)
    return out, tuple(hits)


def redact_tools(tools: list[dict] | None) -> tuple[list[dict] | None, tuple[str, ...]]:
    """Redact secrets that appear inside tool *definitions* (descriptions, parameter text).

    Tools are sent to the provider verbatim, so an embedded key in a schema description would
    otherwise bypass Guard-lite entirely. Structure and semantics are preserved; only string
    leaves inside each definition are scrubbed."""
    if tools is None:
        return None, ()

    def scrub(obj):  # -> (scrubbed, hits)
        if isinstance(obj, str):
            r = redact(obj)
            return r.text, list(r.hits)
        if isinstance(obj, list):
            items, hs = [], []
            for v in obj:
                sv, h = scrub(v)
                items.append(sv)
                hs += [x for x in h if x not in hs]
            return items, hs
        if isinstance(obj, dict):
            out, hs = {}, []
            for k, v in obj.items():
                sv, h = scrub(v)
                out[k] = sv
                hs += [x for x in h if x not in hs]
            return out, hs
        return obj, []

    cleaned, hits = scrub(tools)
    return cleaned, tuple(dict.fromkeys(hits))
