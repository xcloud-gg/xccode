"""`xccode doctor`: verify an installation against its invariants (XC-CODE-001 §15.3, §9, B-49).

Read-only. One line per check; exits non-zero when anything is red. The installer runs it after
`install.sh`, and it is the acceptance gate for a fresh host (B-50: "`xccode doctor` is green").
"""

from __future__ import annotations

from dataclasses import dataclass

NOLOGIN_SHELLS = ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false")
# The operator account on thor (XC-CODE-001 §14, host-spec-thor §14): the human admin who gets the
# `xcc` launcher and the OpenCode profile. `install.sh --operator <login>` adds this account to
# `xccode-users`; the service accounts (`xccode`, `aios`) never are.
OPERATOR = "xcloud"
# Layer 2 (systemd drop-in) and layer 4 (auditd) live in the system directories, not under
# /etc/xcloud/xccode, because that is the only place systemd and auditd read them.
SYSTEMD_DIR = "/etc/systemd/system"
AUDIT_DIR = "/etc/audit/rules.d"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Host:
    """An injectable view of the host, gathered once so checks are pure and testable."""

    passwd: dict[str, tuple[int, int, str]]  # name -> (uid, gid, shell)
    gid_to_group: dict[int, str]  # primary-gid -> group name
    members: dict[str, frozenset[str]]  # group name -> supplementary members
    existing: frozenset[str]  # absolute paths that exist
    etc_dir: str = "/etc/xcloud/xccode"
    state_dir: str = "/var/lib/xcloud/xccode"
    opt_dir: str = "/opt/xcloud/xccode"


def groups_of(host: Host, account: str) -> frozenset[str]:
    """Every group an account belongs to: its primary group plus supplementary memberships."""
    entry = host.passwd.get(account)
    if entry is None:
        return frozenset()
    primary = host.gid_to_group.get(entry[1])
    supp = {g for g, m in host.members.items() if account in m}
    out = set(supp)
    if primary:
        out.add(primary)
    return frozenset(out)


def run_checks(host: Host) -> list[Check]:
    checks: list[Check] = []

    xccode = host.passwd.get("xccode")
    if xccode is None:
        checks.append(Check("account-xccode", False, "the xccode account does not exist"))
    elif xccode[2] not in NOLOGIN_SHELLS:
        checks.append(Check("account-xccode", False, f"xccode has a login shell: {xccode[2]}"))
    else:
        checks.append(Check("account-xccode", True, "xccode exists with no login shell"))

    if OPERATOR not in host.passwd:
        checks.append(Check("account-operator", False, f"{OPERATOR} does not exist"))
    else:
        checks.append(Check("account-operator", True, f"{OPERATOR} exists"))

    if "xccode-users" in host.members:
        members = host.members["xccode-users"]
        if OPERATOR in members:
            checks.append(Check("group-xccode-users", True, f"xccode-users contains {OPERATOR}"))
        else:
            checks.append(Check("group-xccode-users", False, f"{OPERATOR} is not in xccode-users"))
    else:
        checks.append(Check("group-xccode-users", False, "group xccode-users does not exist"))

    xccode_groups = groups_of(host, "xccode") | {"xccode", "xccode-users"}
    for name in ("aios",):
        if name in host.passwd:
            overlap = groups_of(host, name) & xccode_groups
            if overlap:
                checks.append(
                    Check(f"no-{name}-in-xccode", False, f"{name} is in {sorted(overlap)}")
                )
            else:
                checks.append(Check(f"no-{name}-in-xccode", True, f"{name} is in no xccode group"))
        else:
            checks.append(Check(f"no-{name}-in-xccode", True, f"{name} does not exist yet"))

    for label, path, want in (
        ("opt-dir", host.opt_dir, True),
        ("etc-dir", host.etc_dir, True),
        ("state-dir", host.state_dir, True),
    ):
        exists = path in host.existing
        checks.append(Check(label, exists == want, f"{path} {'present' if exists else 'missing'}"))

    nft = f"{host.etc_dir}/nft/xccode.nft"
    nft_present = nft in host.existing
    nft_detail = f"{nft} {'present' if nft_present else 'missing'}"
    checks.append(Check("guard-nft", nft_present, nft_detail))

    audit = f"{AUDIT_DIR}/xccode.rules"
    need_audit = "aios" in host.passwd or "xcloud" in host.passwd
    audit_present = audit in host.existing
    checks.append(
        Check(
            "guard-audit",
            audit_present if need_audit else True,
            f"{audit} {'present' if audit_present else 'missing'}",
        )
    )

    aios = host.passwd.get("aios")
    if aios is not None:
        dropin = f"{SYSTEMD_DIR}/user@{aios[0]}.service.d/40-xccode-paths.conf"
        dropin_present = dropin in host.existing
        dropin_detail = f"{dropin} {'present' if dropin_present else 'missing'}"
        checks.append(Check("guard-dropin", dropin_present, dropin_detail))
    else:
        checks.append(Check("guard-dropin", True, "aiOS does not exist yet; no drop-in needed"))

    return checks


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{'OK  ' if c.ok else 'FAIL'} {c.name:<22} {c.detail}")
    lines.append(f"doctor: {sum(c.ok for c in checks)}/{len(checks)} checks passed")
    return "\n".join(lines)


def gather_passwd_group(
    passwd_text: str, group_text: str
) -> tuple[dict, dict[int, str], dict[str, frozenset[str]]]:
    passwd: dict[str, tuple[int, int, str]] = {}
    for line in passwd_text.splitlines():
        if not line or line.startswith("#"):
            continue
        p = line.split(":")
        if len(p) >= 7:
            try:
                passwd[p[0]] = (int(p[2]), int(p[3]), p[6])
            except ValueError:
                continue
    gid_to_group: dict[int, str] = {}
    members: dict[str, frozenset[str]] = {}
    for line in group_text.splitlines():
        if not line or line.startswith("#"):
            continue
        g = line.split(":")
        if len(g) >= 4:
            try:
                gid = int(g[2])
            except ValueError:
                continue
            gid_to_group[gid] = g[0]
            members[g[0]] = frozenset(m for m in g[3].split(",") if m)
    return passwd, gid_to_group, members
