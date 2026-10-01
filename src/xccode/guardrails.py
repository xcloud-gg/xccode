"""Guardrails: aiOS and xccode never see each other (XC-DES-001 §6.9).

`xccode guard apply` renders layers 2, 3 and 6 for the accounts that exist on the host and
writes them. Layer 1 (permissions) is set by the installer, layer 4 is aiOS's own policy, and
layer 5 is the build agents' managed drop-in — none of those are written here. xccode installs
before aiOS and xcloud exist on a fresh thor, so rules are written only for accounts present in
/etc/passwd; `xccode-guard.path` re-runs this whenever /etc/passwd changes.

The public repository carries no mesh address, aiOS service port, or internal host name (§25.3):
the internal values arrive in a config object the installer fills from /etc/xcloud/xccode.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

# xccode's own loopback services (public, §25.3): xcroute, opencode, OmniRoute, OpenViking, TEI.
XCCODE_PORTS: tuple[int, ...] = (18080, 18090, 18128, 18180, 18181)
# The two services only the xccode account may reach: OpenViking (memory) and TEI (embedder).
MEMORY_PORTS: tuple[int, ...] = (18180, 18181)


@dataclass(frozen=True)
class GuardConfig:
    """Internal policy values. The installer loads these from /etc/xcloud/xccode/guardrails.toml;
    the defaults are empty so the public repository never commits a mesh address or aiOS port."""

    mesh_cidrs: tuple[str, ...] = ()
    aios_ports: tuple[int, ...] = ()
    xccode_ports: tuple[int, ...] = XCCODE_PORTS
    memory_ports: tuple[int, ...] = MEMORY_PORTS


@dataclass(frozen=True)
class GuardWrite:
    """One file `guard apply` produces."""

    path: Path
    content: str


def load_config(path: Path | None) -> GuardConfig:
    """Load internal policy values from a TOML file (the installer writes this from SOPS).

    Missing keys fall back to empty, so an absent or partial file renders the rules that need no
    internal value and omits the rest. Expected shape::

        mesh_cidrs = [".../24"]
        aios_ports = [11434, 8081, 9101]
        xccode_ports = [18080, 18090, 18128, 18180, 18181]
        memory_ports = [18180, 18181]
    """
    if path is None or not path.is_file():
        return GuardConfig()
    data = tomllib.loads(path.read_text())
    return GuardConfig(
        mesh_cidrs=tuple(data.get("mesh_cidrs", ())),
        aios_ports=tuple(data.get("aios_ports", ())),
        xccode_ports=tuple(data.get("xccode_ports", XCCODE_PORTS)),
        memory_ports=tuple(data.get("memory_ports", MEMORY_PORTS)),
    )


def parse_passwd(text: str) -> dict[str, int]:
    """Return ``name -> uid`` from /etc/passwd content (injectable for tests)."""
    uids: dict[str, int] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) >= 3:
            try:
                uids[parts[0]] = int(parts[2])
            except ValueError:
                continue
    return uids


def _set(items: tuple[int, ...]) -> str:
    return "{ " + ", ".join(str(i) for i in items) + " }"


def _set_cidrs(items: tuple[str, ...]) -> str:
    return "{ " + ", ".join(items) + " }"


def render_nft(cfg: GuardConfig, accounts: dict[str, int]) -> str:
    """Render the ``inet xccode`` table (layer 3). Only existing accounts get a rule."""
    aios = accounts.get("aios")
    xccode = accounts.get("xccode")
    lines = [
        "#!/usr/sbin/nft -f",
        "destroy table inet xccode",
        "table inet xccode {",
        "    chain output {",
        "        type filter hook output priority filter; policy accept;",
    ]
    if aios is not None:
        lines.append("        # layer 3: aiOS never reaches xccode's loopback services")
        lines.append(
            f"        meta skuid {aios} tcp dport {_set(cfg.xccode_ports)} counter reject"
        )
    if xccode is not None:
        if cfg.memory_ports:
            lines.append("        # only the xccode account reaches its own memory and embedder")
            lines.append(
                f"        meta skuid != {xccode} tcp dport {_set(cfg.memory_ports)} counter reject"
            )
        if cfg.mesh_cidrs:
            lines.append("        # xccode never reaches the mesh")
            lines.append(
                f"        meta skuid {xccode} ip daddr {_set_cidrs(cfg.mesh_cidrs)} counter reject"
            )
        if cfg.aios_ports:
            lines.append("        # xccode never borrows aiOS's model or telemetry ports")
            lines.append(
                f"        meta skuid {xccode} tcp dport {_set(cfg.aios_ports)} counter reject"
            )
    lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_aios_dropin(aios_uid: int) -> str:
    """Layer 2 belt-and-braces drop-in: hide xcloud paths from aiOS's user manager."""
    return (
        "# xccode layer 2: aiOS's rootless containers never see xcloud paths (E7 v0.2).\n"
        "[Service]\n"
        "InaccessiblePaths=/opt/xcloud /etc/xcloud /var/lib/xcloud\n"
    )


def render_audit_rules(accounts: dict[str, int]) -> str:
    """Layer 6: auditd watches for any access to xccode state by aiOS or xcloud."""
    lines: list[str] = []
    for name in ("aios", "xcloud"):
        uid = accounts.get(name)
        if uid is not None:
            lines.append(
                f"-a always,exit -F dir=/var/lib/xcloud/xccode -F uid={uid} -k xccode-{name}"
            )
    return "\n".join(lines) + ("\n" if lines else "")


def plan(cfg: GuardConfig, accounts: dict[str, int], etc_dir: Path) -> list[GuardWrite]:
    """The files `guard apply` would write, for the accounts that exist."""
    writes: list[GuardWrite] = []
    writes.append(GuardWrite(etc_dir / "nft" / "xccode.nft", render_nft(cfg, accounts)))
    aios = accounts.get("aios")
    if aios is not None:
        dropin = etc_dir / "systemd" / f"user@{aios}.service.d" / "40-xccode-paths.conf"
        writes.append(GuardWrite(dropin, render_aios_dropin(aios)))
    audit = render_audit_rules(accounts)
    if audit:
        writes.append(GuardWrite(etc_dir / "audit" / "xccode.rules", audit))
    return writes


def apply(cfg: GuardConfig, accounts: dict[str, int], etc_dir: Path) -> list[Path]:
    """Write the guardrail files. The installer runs this as root; `--check` previews instead."""
    written: list[Path] = []
    for w in plan(cfg, accounts, etc_dir):
        w.path.parent.mkdir(parents=True, exist_ok=True)
        w.path.write_text(w.content)
        written.append(w.path)
    return written
