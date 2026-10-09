"""Pinned component versions for the installer and `xccode upgrade` (XC-CODE-001 §11).

`etc/versions.lock` (TOML) lists every component with its version and, where the spec demands one, a
checksum or digest. A release that leaves a required pin empty fails `validate`, so an unpinned
download can never ship or upgrade.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

# component -> the digest field that must also be non-empty ("" = version-only pin).
COMPONENTS: dict[str, str] = {
    "opencode": "",                 # npm package @opencode/cli (integrity enforced by npm)
    "omniroute": "",                # pinned release, version only
    "openagentscontrol": "commit",  # pinned commit
    "caveman": "commit",            # skill text pinned at a reviewed commit (§4.14)
    "gitleaks": "sha256",           # pinned release binary
    "hermes": "",
    "openviking": "",
    "tei": "sha256",                # prebuilt text-embeddings-router binary (built at release time)
    "dsh": "",                      # deepseek-harness-sdk
    "debian_image": "sha512",       # the VM/cloud image (Debian publishes sha512)
}


@dataclass(frozen=True)
class Pin:
    component: str
    version: str
    checksum: str


def load(path: Path) -> dict[str, Pin]:
    """Parse a versions.lock TOML file into component -> Pin."""
    data = tomllib.loads(path.read_text())
    pins: dict[str, Pin] = {}
    for name, meta in data.items():
        if not isinstance(meta, dict):
            continue
        field = COMPONENTS.get(name, "")
        checksum = str(meta.get(field, "")) if field else ""
        pins[name] = Pin(name, str(meta.get("version", "")), checksum)
    return pins


def validate(pins: dict[str, Pin]) -> list[str]:
    """Missing or empty pins, and unknown components. Empty list means a shippable lock file."""
    problems: list[str] = []
    for name in sorted(set(pins) - set(COMPONENTS)):
        problems.append(f"unknown component {name}")
    for name in COMPONENTS:
        pin = pins.get(name)
        if pin is None:
            problems.append(f"missing pin for {name}")
            continue
        if not pin.version:
            problems.append(f"{name}: no version")
        if COMPONENTS[name] and not pin.checksum:
            problems.append(f"{name}: no {COMPONENTS[name]}")
    return problems


def is_complete(pins: dict[str, Pin]) -> bool:
    return not validate(pins)
