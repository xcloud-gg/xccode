#!/usr/bin/env python3
"""Repository checks that CI runs on every pull request (public-repository rule, XC-DES-001 §25.3).

1. secret scan: private-key blocks, well-known token shapes, assignments of long literals to
   secret-looking names;
2. "no internal name": no private or mesh IP addresses, no internal domain suffixes, and none of the
   extra names in the XCCODE_INTERNAL_NAMES environment variable (comma separated; CI supplies it
   from a repository secret so the list itself is never committed).

Exit 0 when clean, 1 with a list of findings otherwise.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

SECRET_PATTERNS = {
    "private-key-block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "age-secret-key": re.compile(r"AGE-SECRET-KEY-1[A-Z0-9]{50,}"),
    "github-token": re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "api-key-shape": re.compile(r"\b(sk|rk|pk)-[A-Za-z0-9_-]{24,}\b"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "assigned-secret": re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*['\"][^'\"\s]{16,}['\"]"
    ),
}
INTERNAL_SUFFIX = re.compile(r"\b[\w-]+(\.[\w-]+)*\.(xc0|internal|lan|home\.arpa)\b")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
DOC_NETS = ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
SKIP_SUFFIXES = {".png", ".jpg", ".gif", ".ico", ".pdf", ".gz", ".xz", ".zip", ".whl"}


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split("\n")
    return [ROOT / p for p in out if p and (ROOT / p).is_file()]


def _is_internal_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if any(ip in ipaddress.ip_network(n) for n in DOC_NETS):
        return False  # documentation ranges are fine in examples and tests
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return ip.is_private and not ip.is_loopback and not ip.is_unspecified


def scan_text(name: str, text: str, extra_names: list[str]) -> list[str]:
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, rx in SECRET_PATTERNS.items():
            if rx.search(line):
                findings.append(f"{name}:{lineno}: secret ({label})")
        if INTERNAL_SUFFIX.search(line):
            findings.append(f"{name}:{lineno}: internal domain suffix")
        for m in IPV4.finditer(line):
            if _is_internal_ip(m.group(0)):
                findings.append(f"{name}:{lineno}: private or mesh address {m.group(0)}")
        low = line.lower()
        for n in extra_names:
            if n and n.lower() in low:
                findings.append(f"{name}:{lineno}: internal name from XCCODE_INTERNAL_NAMES")
    return findings


def main() -> int:
    extra = [n.strip() for n in os.environ.get("XCCODE_INTERNAL_NAMES", "").split(",")]
    findings: list[str] = []
    for path in tracked_files():
        if path == SELF or path.suffix in SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        findings += scan_text(str(path.relative_to(ROOT)), text, extra)
    for f in findings:
        print(f)
    print(f"ci_checks: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
