"""Sanitise what the P3-advisor sees: secrets redacted, IPs and MACs pseudonymised (B-51).

The advisor runs on a cloud pool after this transformation. Nothing with a secret, an IP, or a MAC
reaches it: Guard-lite removes secrets, then every IP and MAC is replaced by a stable pseudonym
(`ip-1`, `mac-1`) whose mapping stays local. The exact sanitised payload is stored under
`/var/lib/xcloud/xccode/advisor/` so the operator can audit what left the host; the advisor record
references it by hash (`sent_payload_hash`).
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .hashing import canonical, sha256_hex
from .xcroute.guard import redact

# `(?!\.?\d)` (rather than `(?![\d.])`) so an IP at the end of a sentence — "...192.0.2.1." —
# is still caught, while a 5-octet run like "192.0.2.1.5" is not matched as a prefix.
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\.?\d)")
_MAC = re.compile(r"(?<![0-9A-Fa-f])((?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})(?![0-9A-Fa-f])")
# Best-effort IPv6: a run of hex groups and colons; every candidate is validated with ipaddress.
_IPV6 = re.compile(r"(?<![\w:])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})(?![\w:])")


@dataclass
class Pseudonymiser:
    """Stable address -> pseudonym mapping, kept locally so the operator can reverse it."""

    mapping: dict[str, str] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict)

    def pseudonym(self, kind: str, value: str) -> str:
        if value in self.mapping:
            return self.mapping[value]
        n = self._counters.get(kind, 0) + 1
        self._counters[kind] = n
        name = f"{kind}-{n}"
        self.mapping[value] = name
        return name

    def to_dict(self) -> dict:
        return self.mapping

    @classmethod
    def from_dict(cls, mapping: dict[str, str]) -> Pseudonymiser:
        p = cls(mapping=dict(mapping))
        for name in mapping.values():
            prefix = name.rsplit("-", 1)[0]
            n = int(name.rsplit("-", 1)[1])
            p._counters[prefix] = max(p._counters.get(prefix, 0), n)
        return p


def _pseudonymise(text: str, pseu: Pseudonymiser) -> str:
    def ipv4(m: re.Match[str]) -> str:
        try:
            ipaddress.IPv4Address(m.group(1))
        except ValueError:
            return m.group(0)
        return pseu.pseudonym("ip", m.group(1))

    def mac(m: re.Match[str]) -> str:
        return pseu.pseudonym("mac", m.group(1))

    def ipv6(m: re.Match[str]) -> str:
        try:
            ipaddress.IPv6Address(m.group(1))
        except ValueError:
            return m.group(0)
        return pseu.pseudonym("ip", m.group(1))

    text = _IPV4.sub(ipv4, text)
    text = _MAC.sub(mac, text)
    text = _IPV6.sub(ipv6, text)
    return text


def sanitize(text: str, pseu: Pseudonymiser) -> str:
    """Redact secrets (Guard-lite), then pseudonymise every IP and MAC."""
    return _pseudonymise(redact(text).text, pseu)


def sanitize_advisor_input(inputs: dict, pseu: Pseudonymiser) -> dict:
    """Sanitize every string value of a structured advisor input, recursively, in place (copy)."""
    out: dict = {}
    for key, value in inputs.items():
        if isinstance(value, str):
            out[key] = sanitize(value, pseu)
        elif isinstance(value, dict):
            out[key] = sanitize_advisor_input(value, pseu)
        elif isinstance(value, list):
            out[key] = [
                sanitize_advisor_input(v, pseu) if isinstance(v, dict)
                else sanitize(v, pseu) if isinstance(v, str)
                else v
                for v in value
            ]
        else:
            out[key] = value
    return out


def store_sent(advisor_dir: Path, payload: dict) -> str:
    """Store the exact sanitised payload that left the host; return its content hash (B-51)."""
    data = canonical(payload)
    h = sha256_hex(data)
    path = advisor_dir / f"{h}.sent.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    return h
