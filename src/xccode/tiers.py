"""Deterministic action-tier classifier (P0-P3).

Rules first, no model. The pattern catalogue errs upward: anything that matches no known-safe
P0/P1 pattern is at least P2, and a P3 pattern anywhere in the text (even inside a sub-shell or a
quoted `bash -c` string) raises the whole command to P3.
"""

from __future__ import annotations

import enum
import re
import shlex
from dataclasses import dataclass


class Tier(enum.IntEnum):
    P0 = 0  # read-only inspection
    P1 = 1  # reversible, idempotent
    P2 = 2  # apply a reviewed plan
    P3 = 3  # destructive, irreversible, lock-out, or widening who may act


@dataclass(frozen=True)
class Classification:
    tier: Tier
    rule: str


# --- P3 catalogue: (rule name, regex over the whole command text) -------------------------------
_P3: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(rx, re.IGNORECASE))
    for name, rx in [
        ("tofu-destroy", r"\b(tofu|terraform)\s+(\S+\s+)*destroy\b"),
        ("tofu-apply-destroy", r"\b(tofu|terraform)\s+apply\b.*-destroy\b"),
        ("wipe-or-format", r"\b(wipefs|mkfs(\.\w+)?|blkdiscard|sgdisk|sfdisk|fdisk|parted|gdisk|"
                           r"cryptsetup\s+(luksFormat|erase|luksErase|reencrypt)|"
                           r"zpool\s+(destroy|labelclear)|lvremove|vgremove|pvremove|"
                           r"btrfs\s+(subvolume\s+delete|device\s+(remove|delete)))\b"),
        ("dd-to-device", r"\bdd\b[^|;&]*\bof=/dev/"),
        ("write-to-device", r">\s*/dev/(sd|nvme|vd|hd|mmcblk|dm-|mapper/)"),
        ("shred-device", r"\bshred\b[^|;&]*\s/dev/"),
        ("reinstall", r"\b(debootstrap|pacstrap|installimage|d-i|calamares|"
                      r"xca-disk\s+format|xcloud-install)\b"),
        ("backup-delete", r"\b(restic\s+(forget|prune)|proxmox-backup-client\s+(prune|forget|snapshot\s+forget)|"
                          r"pbs-\w+\s+.*(remove|prune)|borg\s+(delete|prune)|"
                          r"kopia\s+(snapshot\s+delete|maintenance))\b"),
        ("backup-retention", r"\b(keep-(last|daily|weekly|monthly|yearly)|retention|prune-?jobs?)\b.*\b(set|update|edit|change)\b"),
        ("key-rotation", r"\b(sops\s+(updatekeys|rotate)|age-keygen|ssh-keygen\s+.*-(R|k)\b|"
                         r"bao\s+(operator\s+(rekey|rotate|revoke)|lease\s+revoke|token\s+revoke)|"
                         r"vault\s+(operator\s+(rekey|rotate)|lease\s+revoke)|"
                         r"netbird\s+.*(setup-key|key)\s+(revoke|delete)|"
                         r"gpg\s+--(delete|gen-revoke)|"
                         r"update-ca-trust|xca-catrust\s+update)\b"),
        ("ssh-ca-or-authorized-keys", r"(/etc/ssh/(sshd_config|trusted|ca)|authorized_keys|TrustedUserCAKeys|"
                                      r"/etc/sudoers|/etc/pam\.d/)"),
        ("firewall", r"\b(nft\s+(flush|delete|destroy|add\s+rule\s+.*\bdrop\b|-f)|"
                     r"iptables(-nft|-legacy)?\s+(-F|--flush|-P|-D|-I|-A)|ip6tables\s+(-F|-P)|"
                     r"ufw\s+(reset|disable|default|delete|deny)|firewall-cmd\b|"
                     r"systemctl\s+(stop|disable|mask|restart)\s+(nftables|firewalld|ufw|netbird)|"
                     r"ip\s+(-\d\s+)?route\s+(add|del|delete|replace|flush|change)|"
                     r"ip\s+link\s+(set\s+\S+\s+down|del\b|delete\b)|ip\s+(addr|address)\s+(flush|del)|"
                     r"nmcli\s+(con|connection|device|networking)\s+(down|delete|modify|off)|"
                     r"netbird\s+(down|service\s+(stop|uninstall)))\b"),
        ("journal-vacuum", r"\bjournalctl\b[^|;&]*--vacuum"),
        ("firmware-boot", r"\b(fwupdmgr\s+(install|update|downgrade|activate)|flashrom|"
                          r"mokutil\s+--(import|delete|reset|disable|enable|revoke|timeout)|"
                          r"efibootmgr\s+(-[a-zA-Z]*[bBoOnNcCd]|--)|bootctl\s+(install|remove|set-default|update)|"
                          r"grub-install|update-grub|grub-mkconfig|sbctl\s+(enroll|reset|remove)|"
                          r"xca-bootloader\s+update|xca-initramfs\s+update|update-initramfs|"
                          r"kernel-install)\b"),
        ("reboot", r"\b(reboot|shutdown|poweroff|halt|kexec|"
                   r"systemctl\s+(reboot|poweroff|halt|kexec|suspend|hibernate)|init\s+[0-6]|"
                   r"telinit\s+[0-6])\b"),
        ("spend-money", r"\b(hcloud\s+(server|volume|floating-ip|primary-ip)\s+(create|rebuild)|"
                        r"aws\s+ec2\s+run-instances|gcloud\s+compute\s+instances\s+create|"
                        r"doctl\s+compute\s+droplet\s+create|stripe\b|"
                        r"qm\s+(create|clone)|pct\s+(create|clone))\b"),
        ("recursive-delete", r"\brm\b\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z-]+\s+)*"
                             r"(/|~|/\*|/etc|/var|/usr|/boot|/home|/srv|/opt|\$HOME)(\s|/?$|\*)"),
        ("force-push", r"\bgit\s+push\b.*(--force\b|-f\b|--force-with-lease\b|--delete\b|:refs/)"),
        ("destroy-vm-or-storage", r"\b(qm\s+destroy|pct\s+destroy|pvesm\s+free|zfs\s+destroy|"
                                  r"virsh\s+(undefine|destroy)|podman\s+system\s+(reset|prune\s+.*-a)|"
                                  r"docker\s+system\s+prune\s+.*-a)\b"),
        ("account-privilege", r"\b(usermod\s+.*-a?G|gpasswd\s+-a|adduser\s+\S+\s+(sudo|wheel|docker|root)|"
                              r"visudo|passwd\s+(-d|-l)|chage\b|userdel|groupdel|"
                              r"xca-user\s+(add-member|add-group|ensure-group))\b"),
    ]
]

# Edits to the approval machinery itself are P3 by path, whatever the command is.
_P3_PATHS = re.compile(
    r"(/etc/xcloud/xccode/|/opt/xcloud/xccode/|approver\.hash|/var/lib/xcloud/xccode/(advisor|audit|plans)/|"
    r"(^|/)pp/[A-Za-z0-9_.-]+/|paved[-_]path|approval[-_]?(rules|policy)|profiles?/[A-Za-z0-9_.-]+\.(ya?ml|toml))",
)
_WRITES = re.compile(
    r"(>>?|\btee\b|\bsed\s+(-[a-zA-Z]*i|--in-place)|\bcp\b|\bmv\b|\brm\b|\bln\b|\binstall\b|"
    r"\bchmod\b|\bchown\b|\btruncate\b|\bxca-(install|remove)\b|\brsync\b|\bgit\s+(apply|checkout|commit|rm|mv)\b)"
)

# --- P1: reversible and idempotent ----------------------------------------------------------------
_P1 = [
    re.compile(rx)
    for rx in [
        r"^systemctl\s+(--user\s+)?(reload|restart|try-restart|reload-or-restart|daemon-reload|start)\b",
        r"^xca-systemctl\s+(reload|restart|start|daemon-reload)\b",
        r"^ansible(-playbook)?\b.*\s(--check|-C)\b",
        r"^(tofu|terraform)\s+(plan|validate|fmt)\b",
        r"^nft\s+-c\b",
        r"^xca-nft\s+check\b",
        r"^caddy\s+validate\b",
        r"^visudo\s+-c\b",
        r"^systemd-analyze\s+verify\b",
        r"\s(--dry-run|--check)(\s|$)",
        r"^(pytest|ruff|python3?\s+-m\s+(pytest|ruff)|shellcheck|bash\s+-n)\b",
        r"^git\s+(fetch|stash|add|commit|switch|worktree)\b",
    ]
]

# --- P0: read-only -------------------------------------------------------------------------------
_P0_CMDS = {
    "ls", "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "ug", "ugrep",
    "stat", "file", "wc", "df", "du", "free", "uptime", "uname", "id", "whoami", "hostname",
    "hostnamectl", "date", "env", "printenv", "echo", "printf", "pwd", "which", "type", "readlink",
    "realpath", "basename", "dirname", "sort", "uniq", "cut", "tr", "column", "jq", "yq", "diff",
    "cmp", "md5sum", "sha256sum", "sha512sum", "lsblk", "blkid", "findmnt", "lsmod", "lspci",
    "lsusb", "lscpu", "lsof", "ps", "pgrep", "top", "htop", "ss", "ping", "dig", "host", "nslookup",
    "getent", "mount", "ip", "journalctl", "dmesg", "lsns", "nproc", "sensors", "smartctl",
    "nvidia-smi", "xca-journal", "true", "false", "test", "[", "[[", "tree", "eza", "bat", "dpkg",
    "apt-cache", "apt", "pacman", "netbird", "wg", "resolvectl", "timedatectl", "loginctl",
    "systemctl", "xca-systemctl", "git", "nft", "xca-nft", "xca-user", "xca-pkg", "mokutil",
    "fwupdmgr", "efibootmgr",
}
# subcommand gating for commands that are only read-only in some forms
_P0_SUB = {
    "systemctl": {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files",
                  "list-timers", "list-failed", "show", "cat", "list-dependencies", "--version"},
    "xca-systemctl": {"status", "is-active", "is-enabled", "list-failed", "show", "cat"},
    "git": {"status", "log", "diff", "show", "rev-parse", "describe", "ls-files", "blame", "grep",
            "remote", "config", "tag", "branch", "ls-remote", "shortlog", "reflog"},
    "ip": {"addr", "address", "a", "-br", "-brief", "route", "r", "link", "l", "neigh", "rule", "-s",
           "-j", "-4", "-6", "-d"},
    "nft": {"list"},
    "xca-nft": {"list", "check"},
    "xca-user": {"info"},
    "xca-pkg": {"policy"},
    "dpkg": {"-l", "-L", "-s", "-S", "-p", "--list", "--status", "--listfiles", "--search", "--print-avail"},
    "apt": {"list", "show", "search", "policy", "depends", "rdepends", "--version"},
    "apt-cache": {"policy", "show", "search", "depends", "rdepends", "madison"},
    "pacman": {"-Q", "-Qi", "-Ql", "-Qs", "-Ss", "-Si", "-Qe", "-Qk"},
    "netbird": {"status", "version"},
    "hostnamectl": {"status", "show", "", "--version", "--static", "--transient", "--pretty", "--json"},
    "mokutil": {"--sb-state", "--list-enrolled", "--db", "--pk", "--kek", "--list-new"},
    "fwupdmgr": {"get-devices", "get-updates", "get-history", "get-plugins", "--version"},
    "efibootmgr": {"-v", "--verbose", ""},
    "mount": {""},
    "loginctl": {"list-sessions", "show-session", "session-status", "list-users", "show-user", "status"},
    "timedatectl": {"status", "show", "timesync-status"},
    "resolvectl": {"status", "query", "statistics"},
    "wg": {"show", "showconf"},
}
_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||&|\n")
_SUBSHELL = re.compile(r"\$\(|`|<\(|>\(")
# `ip` is read-only only in its show forms; these verbs make it a write (the destructive forms are
# already in the P3 catalogue above, so this is the reversible-write floor).
_IP_WRITE = re.compile(r"\b(add|del|delete|set|change|replace|flush|up|down)\b")
# awk/sed are read-only only when their program cannot execute anything or spawn a sub-process.
_INTERP_EXEC = re.compile(r"\b(system|exec)\s*\(|\bgetline\b|\|\s*[\"']")
_WRAPPERS = {"sudo", "env", "nohup", "time", "timeout", "nice", "ionice", "stdbuf", "command", "exec",
             "xca-aios"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return segment.split()


def _unwrap(tokens: list[str]) -> tuple[list[str], str | None]:
    """Strip sudo/env/ssh style prefixes; return (inner tokens, nested script text if any)."""
    toks = list(tokens)
    while toks:
        head = toks[0]
        if "=" in head and not head.startswith("-") and re.match(r"^[A-Za-z_]\w*=", head):
            toks.pop(0)
            continue
        if head in _WRAPPERS:
            toks.pop(0)
            # drop wrapper options (and timeout's duration)
            while toks and (toks[0].startswith("-") or (head == "timeout" and re.match(r"^\d", toks[0]))):
                toks.pop(0)
            continue
        if head in {"ssh", "mosh"}:
            toks.pop(0)
            while toks and toks[0].startswith("-"):
                opt = toks.pop(0)
                if opt in {"-i", "-p", "-o", "-l", "-J", "-F", "-L", "-R", "-D", "-b", "-c", "-E"} and toks:
                    toks.pop(0)
            if toks:
                toks.pop(0)  # host
            return toks, " ".join(toks) if toks else None
        break
    if toks and toks[0].split("/")[-1] in _SHELLS:
        for i, t in enumerate(toks):
            if t == "-c" or (t.startswith("-") and t.endswith("c") and not t.startswith("--")):
                if i + 1 < len(toks):
                    return toks, toks[i + 1]
        return toks, None
    return toks, None


def _git_listing(sub: str, args: list[str]) -> Classification:
    """`git config|remote|tag|branch|reflog` only read when they list or get; else writes."""
    plain = [t for t in args if not t.startswith("-")]
    if sub == "config" and any(t in {"--get", "--list", "-l", "--get-all", "--get-regexp"} for t in args):
        return Classification(Tier.P0, "p0:git-config-read")
    if sub in {"tag", "branch"} and not plain and not any(
        t in {"-d", "-D", "-m", "-M", "-f", "--delete", "-c", "-C", "-u", "--set-upstream-to"} for t in args
    ):
        return Classification(Tier.P0, f"p0:git-{sub}-list")
    if sub == "remote" and (not plain or plain[0] in {"show", "get-url"}):
        return Classification(Tier.P0, "p0:git-remote-read")
    if sub == "reflog" and (not plain or plain[0] in {"show", "exists"}):
        return Classification(Tier.P0, "p0:git-reflog-read")
    return Classification(Tier.P2, f"git-{sub}-writes")


def _segment_tier(segment: str, remote_hint: bool) -> Classification:
    segment = segment.strip()
    if not segment:
        return Classification(Tier.P0, "empty")
    toks, nested = _unwrap(_tokens(segment))
    remote = remote_hint or segment.lstrip().startswith(("ssh ", "mosh "))
    if nested is not None and (toks and toks[0].split("/")[-1] in _SHELLS):
        inner = classify(nested, remote=remote)
        return Classification(max(inner.tier, Tier.P2), f"shell-c:{inner.rule}")
    if not toks:
        return Classification(Tier.P2 if remote else Tier.P0, "wrapper-only")
    cmd = toks[0].split("/")[-1]
    rest = " ".join(toks)
    # output redirection to anything but /dev/null is a write
    cleaned = re.sub(r"\d*>\s*&\d|\d*>\s*/dev/null|2>&1", "", segment)
    writes = bool(re.search(r">>?", cleaned))
    for rx in _P1:
        if rx.search(rest):
            return Classification(Tier.P1, "p1:" + rx.pattern[:40])
    if cmd == "ip" and _IP_WRITE.search(rest):
        return Classification(Tier.P2, "ip-write")
    if cmd in _P0_CMDS and not writes:
        sub = _P0_SUB.get(cmd)
        if sub is None:
            return Classification(Tier.P0, f"p0:{cmd}")
        first = next((t for t in toks[1:] if not t.startswith("-") or t in sub), "")
        if first in sub:
            if cmd == "git" and first in {"config", "remote", "tag", "branch", "reflog"}:
                return _git_listing(first, toks[2:])
            return Classification(Tier.P0, f"p0:{cmd}-{first or 'list'}")
    if cmd == "find" and not writes and not re.search(r"-(delete|exec|execdir|ok|fprint|fls)\b", rest):
        return Classification(Tier.P0, "p0:find")
    if cmd in {"sed", "awk"} and not writes and "-i" not in toks[1:] and "--in-place" not in rest \
            and not _INTERP_EXEC.search(rest):
        return Classification(Tier.P0, f"p0:{cmd}-read")
    return Classification(Tier.P2, "unknown-or-write")


def _touches_protected_path(text: str) -> bool:
    return bool(_P3_PATHS.search(text)) and bool(_WRITES.search(text))


def classify(command: str, *, remote: bool = False) -> Classification:
    """Return the tier of a shell command line (the maximum over its parts)."""
    text = command.strip()
    for name, rx in _P3:
        if rx.search(text):
            return Classification(Tier.P3, name)
    if _touches_protected_path(text):
        return Classification(Tier.P3, "protected-path-write")
    worst = Classification(Tier.P0, "empty")
    if _SUBSHELL.search(text):
        worst = Classification(Tier.P2, "subshell")
    for seg in _SEGMENT_SPLIT.split(text):
        c = _segment_tier(seg, remote)
        if c.tier > worst.tier:
            worst = c
        elif worst.rule == "empty" and c.tier == worst.tier:
            worst = c
    return worst


def classify_plan(steps: list[dict]) -> Classification:
    """Plan tier = the highest tier of its steps. Each step: {"cmd": str, "remote": bool?}."""
    worst = Classification(Tier.P0, "no-steps")
    for s in steps:
        c = classify(s["cmd"], remote=bool(s.get("remote")))
        if c.tier > worst.tier:
            worst = c
    return worst
