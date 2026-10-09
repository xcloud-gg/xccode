"""Operator-facing commands for the xccode CLI (XC-CODE-001 §4.16): status, secrets, review,
keep, report, upgrade, export.

All read-only except where the command name says otherwise; everything here keeps the public-repo
rule (no secrets, hosts, or mesh values). State lives under $XCCODE_STATE ($STATE).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

from .config import configured_path
from .versions import COMPONENTS
from .versions import load as load_pins
from .versions import loads as parse_pins

DEFAULT_ETC = Path("/etc/xcloud/xccode")


ETC = configured_path("XCCODE_ETC", DEFAULT_ETC)
STATE = configured_path("XCCODE_STATE", "/var/lib/xcloud/xccode")
OPT = configured_path("XCCODE_OPT", "/opt/xcloud/xccode")

SERVICES = ("xcroute", "omniroute", "openviking", "tei")
SYSTEM_TIMERS = ("xccode-learn", "xccode-bench", "xccode-backup")
PORTS = {
    18080: "xcroute", 18090: "opencode serve", 18128: "omniroute",
    18180: "openviking", 18181: "tei",
}
KEPT_DAYS = 180


def _systemctl(*args: str) -> str:
    r = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=15)
    return r.stdout.strip() or r.stderr.strip()


def _is_active(unit: str) -> bool:
    r = subprocess.run(
        ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10
    )
    return r.stdout.strip() == "active"


def _port_listens(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


# --- xccode status (P0): health of every service + timers ---------------------------------

def status() -> str:
    """One line per service/timer/port; ends with an overall line (XC-CODE-001 §4.16).

    Read-only. A host before Alloy/ntfy exists (xCloud L2) shows *degraded* detection per §9 —
    the guardrails still work, only alerting is local."""
    lines: list[str] = []
    ok = 0
    for svc in SERVICES + tuple(f"{t}.timer" for t in SYSTEM_TIMERS):
        unit = svc if svc.endswith((".service", ".timer")) else f"{svc}.service"
        up = _is_active(unit)
        lines.append(f"{'OK  ' if up else 'FAIL'} {unit:<24} {'active' if up else 'not active'}")
        ok += up and 1 or 0
    for port, name in PORTS.items():
        up = _port_listens(port)
        lines.append(f"{'OK  ' if up else 'FAIL'} {name:<24} {'listening' if up else 'down'} "
                     f"(127.0.0.1:{port})")
        ok += up and 1 or 0
    # §9 L4 detection is local until Alloy+ntfy exist; report rather than fail.
    ntfy = shutil.which("ntfy") is not None or Path("/etc/alloy").exists()
    lines.append(f"{'OK  ' if ntfy else 'WARN'} l4-alerting            "
                 f"{'wired' if ntfy else 'local only (xCloud L2 missing) — degraded'}")
    total = len(lines)
    ok += ntfy and 1 or 0
    lines.append(f"status: {ok}/{total} healthy")
    return "\n".join(lines)


def cmd_status(args) -> int:
    text = status()
    print(text)
    ok = sum(1 for line in text.splitlines() if line.startswith(("OK", "WARN")))
    total = sum(1 for line in text.splitlines() if line[:4] in ("OK  ", "FAIL", "WARN"))
    return 0 if ok == total else 1


# --- xccode secrets set <name> (systemd-creds, §4.16) ---------------------------------------

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CREDSTORE = configured_path("XCCODE_CREDSTORE", "/etc/credstore.encrypted")


def _drop_to_xccode() -> None:
    """Drop root -> the xccode account for $STATE commands (advisor C2).

    No-op when not root or when xccode does not exist (dev hosts/tests)."""
    if os.geteuid() != 0:
        return
    import pwd

    try:
        pw = pwd.getpwnam("xccode")
    except KeyError:
        return
    os.setgroups([])
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    os.environ["HOME"] = pw.pw_dir


def _safe_dest(path: Path) -> None:
    """Refuse a destination that exists as a symlink (lstat does not follow)."""
    st = os.lstat(path) if os.path.lexists(path) else None
    if st is not None and os.path.islink(path):
        raise SystemExit(f"refusing symlinked destination: {path}")


def _write_new(path: Path, data: str) -> None:
    """Create a file without following links, mode 0600, failing if it exists."""
    _safe_dest(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data.encode())
    finally:
        os.close(fd)


def _read_secret() -> str:
    """Read the secret from getpass on a TTY (never echoed) or stdin otherwise."""
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass("secret: ")
    return sys.stdin.read().rstrip("\n")


def secrets_set(name: str) -> int:
    """Encrypt a key from stdin into a host-bound credential via systemd-creds.

    The secret never passes through argv or the environment, and the file lands in a root-owned
    0700 credstore with no symlink followed (advisor C2)."""
    creds = shutil.which("systemd-creds")
    if creds is None:
        print("secrets: systemd-creds not found", file=sys.stderr)
        return 1
    if not _NAME.match(name):
        print(f"secrets: bad name {name!r} ([A-Za-z0-9_.-] up to 64)", file=sys.stderr)
        return 2
    dest = CREDSTORE
    dest.mkdir(parents=True, exist_ok=True)  # /etc/credstore* is root:root on a normal install
    out = dest / f"{name}.cred"
    if os.path.lexists(out):
        if os.path.islink(out):
            print(f"secrets: refusing symlinked destination {out}", file=sys.stderr)
        else:
            print(f"secrets: {name} exists; remove it first to rotate", file=sys.stderr)
        return 1
    secret = _read_secret()
    r = subprocess.run(
        [creds, "encrypt", "--with-key=host+tpm2", "--name", name, "-", str(out)],
        input=secret, capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"secrets: systemd-creds failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    os.chmod(out, 0o600)
    print(f"secrets: stored {name} (host-bound; unreadable off this machine)")
    return 0


# --- xccode keep <request_id> (§4.16, §8) ----------------------------------------------------

def keep(request_id: str, state_dir: Path = STATE) -> int:
    """Save one redacted routing event for later labelling/replay; expires after 180 days."""
    if not request_id.isdigit():
        print(f"keep: request id must be digits only, got {request_id!r}", file=sys.stderr)
        return 2
    _drop_to_xccode()
    db = state_dir / "events.db"
    if not db.is_file():
        print("keep: no events db", file=sys.stderr)
        return 1
    rid = int(request_id)
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
        row = c.execute("SELECT ts, body FROM routing_events WHERE id=?", (rid,)).fetchone()
    if row is None:
        print(f"keep: no routing event {rid}", file=sys.stderr)
        return 1
    kept = state_dir / "kept"
    kept.mkdir(parents=True, exist_ok=True)
    entry = {
        "request_id": rid,
        "ts": row[0],
        "body": json.loads(row[1]),
        "kept_at": time.strftime("%Y-%m-%d", time.gmtime()),
        "expires_after_days": KEPT_DAYS,
    }
    _write_new(kept / f"{rid}.json", json.dumps(entry, indent=2, sort_keys=True) + "\n")
    print(f"keep: saved request {rid} (expires after {KEPT_DAYS} days)")
    return 0


# --- xccode report (§8) -----------------------------------------------------------------------

def _db(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def report(state_dir: Path = STATE, days: int = 7) -> str:
    """Weekly summary: request/event counts, spend by agent, top outcomes, pending learn queue."""
    db = state_dir / "events.db"
    lines = [f"xccode weekly report (last {days} days)", ""]
    if db.is_file():
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
        # Read-only: as any user this must never create -wal/-shm beside events.db (advisor C2).
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
            total = c.execute(
                "SELECT count(*) FROM routing_events WHERE ts >= ?", (cutoff,)
            ).fetchone()[0]
            by_agent = c.execute(
                "SELECT agent, count(*) FROM routing_events WHERE ts >= ? "
                "GROUP BY agent ORDER BY 2 DESC", (cutoff,),
            ).fetchall()
            by_pool = c.execute(
                "SELECT pool, count(*) FROM routing_events WHERE ts >= ? AND pool IS NOT NULL "
                "GROUP BY pool ORDER BY 2 DESC", (cutoff,),
            ).fetchall()
            refused = c.execute(
                "SELECT count(*) FROM routing_events WHERE ts >= ? AND outcome != 'ok'",
                (cutoff,),
            ).fetchone()[0]
        lines += [f"requests: {total} ({refused} refused/failed)"]
        lines.append("by agent:  " + (", ".join(f"{a}={n}" for a, n in by_agent) or "—"))
        lines.append("by pool:   " + (", ".join(f"{p}={n}" for p, n in by_pool) or "—"))
    else:
        lines.append("requests: (no events db)")
    pending = list((state_dir / "learn" / "pending").glob("*.json")) if state_dir else []
    review = list((state_dir / "learn" / "review").glob("*.json")) if state_dir else []
    kept = list((state_dir / "kept").glob("*.json")) if state_dir else []
    lines += [
        f"learn queue: {len(pending)} pending · {len(review)} awaiting review",
        f"kept turns:  {len(kept)} (kept turns expire after {KEPT_DAYS} days)",
    ]
    return "\n".join(lines)


# --- xccode review (§6.6) ---------------------------------------------------------------------

def _sanitize(text: object) -> str:
    """Strip terminal control/escape sequences from model-written content before printing."""
    return "".join(c for c in str(text) if c.isprintable() or c in "\n\t")


def _review_item(item_id: str, d: Path) -> Path | None:
    """Resolve a queue item by exact id or unambiguous prefix; never leaves the queue dir."""
    if not _ITEM_ID.match(item_id):
        return None
    exact = d / f"{item_id}.json"
    if exact.is_file():
        return exact
    matches = [p for p in d.glob(f"{item_id}*.json")]
    if len(matches) != 1:
        return None
    p = matches[0].resolve()
    return p if p.parent.resolve() == d.resolve() else None


def review_list(state_dir: Path = STATE) -> int:
    d = state_dir / "learn" / "review"
    items = sorted(d.glob("*.json")) if d.is_dir() else []
    if not items:
        print("review: nothing awaiting review")
        return 0
    for it in items:
        body = json.loads(it.read_text())
        kind = body.get("kind", "?")
        text = body.get("content", body.get("name", ""))
        print(f"{it.stem}  {kind:<5}  {_sanitize(text)[:80]}")
    print(f"review: {len(items)} item(s); inspect with `xccode review show <id>`, "
          "decide with `xccode review approve|reject <id>`")
    return 0


def review_show(item_id: str, state_dir: Path = STATE) -> int:
    """Show the full pending item with control characters stripped (never a truncated view)."""
    d = state_dir / "learn" / "review"
    if not d.is_dir():
        print("review: no queue", file=sys.stderr)
        return 1
    p = _review_item(item_id, d)
    if p is None:
        print(f"review: no unambiguous item {item_id!r}", file=sys.stderr)
        return 1
    print(_sanitize(p.read_text()), end="")
    print(f"\nreview: {p.stem} (full content above; inspect before deciding)")
    return 0


def review_decide(item_id: str, approve: bool, state_dir: Path = STATE) -> int:
    _drop_to_xccode()
    d = state_dir / "learn" / "review"
    if not d.is_dir():
        print("review: no queue", file=sys.stderr)
        return 1
    src = _review_item(item_id, d)
    if src is None:
        print(f"review: no unambiguous item {item_id!r}", file=sys.stderr)
        return 1
    # The operator approves only after the full content has been shown — never off a truncated
    # 80-char preview, where injected instructions could hide past the cut (advisor C3).
    print(_sanitize(src.read_text()), end="")
    print(f"\nreview: ^^ full content of {src.stem} — approving/rejecting exactly this")
    dst_dir = state_dir / "learn" / ("approved" if approve else "rejected")
    dst_dir.mkdir(parents=True, exist_ok=True)
    src.replace(dst_dir / src.name)
    print(f"review: {src.stem} {'approved' if approve else 'rejected'} → {dst_dir.name}/")
    if approve:
        # An approved rule joins the dated rules tier every ctx_brief includes; an approved skill
        # installs as a native OpenCode skill (§6.6). The learn run consumes learn/approved/.
        print("review: an approved rule joins rules/ and an approved skill installs into "
              "skills/approved/ on the next learn run")
    return 0


# --- xccode upgrade (§11) -----------------------------------------------------------------

INSTALLED_LOCK = OPT / "versions.installed.lock"


def _read_apply_pins(path: Path) -> tuple[dict, str]:
    """Read an apply candidate without following links or trusting a writable lock file."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("pins must be a regular file")
        if info.st_size > 65536:
            raise ValueError("pins file exceeds 64 KiB")
        if info.st_mode & 0o022:
            raise ValueError("pins file must not be group- or world-writable")
        allowed_owners = {0, os.getuid()}
        sudo_uid = os.environ.get("SUDO_UID", "")
        if sudo_uid.isdigit():
            allowed_owners.add(int(sudo_uid))
        if info.st_uid not in allowed_owners:
            raise ValueError("pins file has an untrusted owner")
        chunks = []
        total = 0
        with os.fdopen(fd, "rb") as source:
            fd = -1
            while total <= 65536:
                chunk = source.read(min(8192, 65537 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        if total > 65536:
            raise ValueError("pins file exceeds 64 KiB")
        raw = b"".join(chunks)
        text = raw.decode("utf-8")
        return parse_pins(text), text
    finally:
        if fd >= 0:
            os.close(fd)


def _write_lock_atomic(path: Path, content: str) -> None:
    """Atomically replace a root-controlled lock without following a planted symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    dir_fd = os.open(path.parent, flags)
    temp_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = -1
    created_temp = False
    try:
        info = os.fstat(dir_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PermissionError("versions.lock directory must be owned by the applying user")
        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(temp_name, create_flags, 0o600, dir_fd=dir_fd)
        created_temp = True
        view = memoryview(content.encode("utf-8"))
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fchown(fd, os.geteuid(), os.getegid())
        os.fchmod(fd, 0o644)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        if created_temp:
            try:
                os.unlink(temp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(dir_fd)


def _require_root_for_upgrade() -> bool:
    if os.geteuid() == 0:
        if configured_path("XCCODE_ETC", DEFAULT_ETC) != DEFAULT_ETC:
            print("upgrade: --apply refuses a non-default XCCODE_ETC; unset the override so the "
                  "installer and CLI use the same lock path", file=sys.stderr)
            return False
        return True
    print("upgrade: --apply must run as root (for example, sudo xccode upgrade --apply); "
          "versions.lock is operator-controlled", file=sys.stderr)
    return False

def upgrade(apply: bool, pins_path: Path | None = None) -> int:
    """Show installed vs candidate pins; root-only --apply writes the installer's lock (§11).

    Does not fetch or install. The candidate is an explicit ``--pins`` file, else the operator's
    /etc lock, else the currently installed lock. Applying replaces $ETC/versions.lock, which the
    root installer reads on re-run. Root is required so the xccode account cannot stage a lock that
    a later root install would trust.
    """
    if apply and not _require_root_for_upgrade():
        return 2
    lock = pins_path or (ETC / "versions.lock" if (ETC / "versions.lock").is_file()
                         else INSTALLED_LOCK)
    if not lock.is_file():
        print(f"upgrade: no versions.lock at {lock}", file=sys.stderr)
        return 1
    try:
        if apply:
            pins, text = _read_apply_pins(lock)
        else:
            pins = load_pins(lock)
            text = lock.read_text()
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"upgrade: cannot read pins: {exc}", file=sys.stderr)
        return 1
    installed = load_pins(INSTALLED_LOCK) if INSTALLED_LOCK.is_file() else {}
    print(f"{'component':<18}{'installed':<24}{'pinned':<24}")
    for name in COMPONENTS:
        cur = installed.get(name)
        new = pins.get(name)
        marker = " " if (cur and new and cur.version == new.version) else "*"
        print(f"{marker}{name:<18}{(cur.version if cur else '—'):<24}"
              f"{(new.version if new else '—'):<24}")
    if apply:
        from .versions import validate

        problems = validate(pins)
        if problems:
            for p in problems:
                print(f"upgrade: refusing — {p}", file=sys.stderr)
            return 1
        target = ETC / "versions.lock"
        try:
            _write_lock_atomic(target, text)
        except OSError as exc:
            print(f"upgrade: could not write {target}: {exc}", file=sys.stderr)
            return 1
        print(f"upgrade: reviewed pins written to {target}; re-run install.sh to apply")
    else:
        print("upgrade: diff shown; apply with --apply (the operator then re-runs install.sh)")
    return 0


# --- xccode export [--aios] (§8) --------------------------------------------------------------

def export(aios: bool, out_dir: Path, state_dir: Path = STATE) -> int:
    """Export routing events (+schema) as Parquet (JSONL when pyarrow is absent).

    --aios renames the xc_ fields to the aiOS §5.11 names so aiOS imports the file unchanged.
    Writes are 0600 and never through a planted symlink. (Only events are exported; bench scores
    live in serve.toml, skills in $OPT — copying those stays a deliberate operator action.)"""
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        pa = None
    db = state_dir / "events.db"
    _safe_dest(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # xc -> aiOS §5.11 field rename (--aios only; absent values stay null, never fabricated)
    AIOS_MAP = {"xc_agent_role": "agent_role", "xc_repo_class": "repo_class",
                "xc_decider": "decider", "tool_count": "xc_tool_count"}
    rows: list[dict] = []
    if db.is_file():
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:  # never create wal files
            cur = c.execute(
                "SELECT id, ts, agent, decider, pool, outcome, body FROM routing_events "
                "ORDER BY id"
            )
            for rid, ts, agent, decider, pool, outcome, body in cur:  # streamed, not buffered
                b = json.loads(body)
                row = {
                    "id": rid, "ts": ts, "agent": agent, "decider": decider,
                    "pool": pool, "outcome": outcome,
                    "xc_agent_role": b.get("xc_agent_role"),
                    "xc_repo_class": b.get("xc_repo_class"),
                    "xc_decider": b.get("xc_decider"),
                    "cost_micro_usd": b.get("cost_micro_usd", 0),
                    "tokens_in": b.get("tokens_in", 0),
                    "tokens_out": b.get("tokens_out", 0),
                    "latency_ms": b.get("latency_ms", 0),
                    "tool_count": b.get("tool_count", 0),
                    "guard_hits": ",".join(b.get("guard_hits", [])),
                }
                if aios:
                    row = {AIOS_MAP.get(k, k): v for k, v in row.items()}
                rows.append(row)
    schema = [
        ("id", "int64"), ("ts", "string"), ("agent", "string"), ("decider", "string"),
        ("pool", "string"), ("outcome", "string"), ("xc_agent_role", "string"),
        ("xc_repo_class", "string"), ("xc_decider", "string"), ("cost_micro_usd", "int64"),
        ("tokens_in", "int64"), ("tokens_out", "int64"), ("latency_ms", "int64"),
        ("tool_count", "int64"), ("guard_hits", "string"),
    ]
    if pa is None:
        # No pyarrow in the venv: emit Parquet's twin, JSON Lines + schema, and say so.
        jq = out_dir / "xccode-events.jsonl"
        with open(jq, "w") as f:
            for r in rows:
                f.write(json.dumps({k: (v if not isinstance(v, Path) else str(v))
                                    for k, v in r.items()}, sort_keys=True) + "\n")
        (out_dir / "schema.json").write_text(json.dumps({
            "format": "parquet", "fallback": "jsonl (pyarrow missing)",
            "columns": [{"name": n, "type": t} for n, t in schema],
        }, indent=2) + "\n")
        print(f"export: wrote {jq} + schema.json (add pyarrow for real Parquet)")
        return 0
    table = pa.Table.from_pylist(rows or [{k: None for k, _ in schema}])
    # Empty frame: build the typed empty table so the schema is still usable downstream.
    if not rows:
        table = None
    if rows:
        pq.write_table(table, out_dir / "xccode-events.parquet")
    (out_dir / "schema.json").write_text(json.dumps({
        "schema": "aios" if aios else "xccode",
        "columns": [{"name": n, "type": t} for n, t in schema],
    }, indent=2) + "\n")
    print(f"export: {len(rows)} events → {out_dir}"
          + (" (aiOS field names)" if aios else ""))
    return 0


# --- dispatch (wired in cli.py) ----------------------------------------------------------------

def cmd_secrets(args) -> int:
    if args.action == "set":
        return secrets_set(args.name)
    print(f"secrets: unknown action {args.action!r}", file=sys.stderr)
    return 2


def cmd_review(args) -> int:
    _drop_to_xccode()
    if args.verb is None:
        return review_list()
    if not args.item:
        print("review: an item id is required", file=sys.stderr)
        return 2
    if args.verb == "show":
        return review_show(args.item)
    if args.verb not in ("approve", "reject"):
        print("review: pass approve or reject (or show)", file=sys.stderr)
        return 2
    return review_decide(args.item, approve=(args.verb == "approve"))


def cmd_keep(args) -> int:
    return keep(args.request_id)


def cmd_report(args) -> int:
    _drop_to_xccode()
    print(report(days=args.days))
    return 0


def cmd_upgrade(args) -> int:
    return upgrade(apply=args.apply, pins_path=args.pins)


def cmd_export(args) -> int:
    _drop_to_xccode()
    out_value = args.out or os.environ.get("XCCODE_EXPORT_DIR") or str(STATE / "export")
    out = Path(out_value).expanduser()
    return export(aios=args.aios, out_dir=out)
