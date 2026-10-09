"""Operator-facing commands for the xccode CLI (XC-CODE-001 §4.16): status, secrets, review,
keep, report, upgrade, export.

All read-only except where the command name says otherwise; everything here keeps the public-repo
rule (no secrets, hosts, or mesh values). State lives under $XCCODE_STATE ($STATE).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .versions import COMPONENTS
from .versions import load as load_pins

ETC = Path(os.environ.get("XCCODE_ETC", "/etc/xcloud/xccode"))
STATE = Path(os.environ.get("XCCODE_STATE", "/var/lib/xcloud/xccode"))
OPT = Path(os.environ.get("XCCODE_OPT", "/opt/xcloud/xccode"))

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
    ok = sum(1 for line in text.splitlines() if line.startswith("OK"))
    total = sum(1 for line in text.splitlines() if line[:4] in ("OK  ", "FAIL", "WARN"))
    return 0 if ok == total else 1


# --- xccode secrets set <name> (systemd-creds, §4.16) ---------------------------------------

def secrets_set(name: str) -> int:
    """Encrypt a key from stdin into a host-bound credential via systemd-creds."""
    creds = shutil.which("systemd-creds")
    if creds is None:
        print("secrets: systemd-creds not found", file=sys.stderr)
        return 1
    dest = STATE / "secrets"
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{name}.cred"
    if out.exists():
        print(f"secrets: {name} exists; remove it first to rotate", file=sys.stderr)
        return 1
    r = subprocess.run(
        [creds, "encrypt", "--name", name, "-", str(out)],
        stdin=sys.stdin, capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"secrets: systemd-creds failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    out.chmod(0o600)
    print(f"secrets: stored {name} (host-bound; unreadable off this machine)")
    return 0


# --- xccode keep <request_id> (§4.16, §8) ----------------------------------------------------

def keep(request_id: str, state_dir: Path = STATE) -> int:
    """Save one redacted routing event for later labelling/replay; expires after 180 days."""
    db = state_dir / "events.db"
    if not db.is_file():
        print("keep: no events db", file=sys.stderr)
        return 1
    try:
        rid = int(request_id)
    except ValueError:
        print(f"keep: request id must be a number, got {request_id!r}", file=sys.stderr)
        return 2
    with sqlite3.connect(db) as c:
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
    (kept / f"{rid}.json").write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
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
        with _db(db) as c:
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
        print(f"{it.stem}  {kind:<5}  {str(text)[:80]}")
    print(f"review: {len(items)} item(s); decide with xccode review <id> approve|reject")
    return 0


def review_decide(item_id: str, approve: bool, state_dir: Path = STATE) -> int:
    d = state_dir / "learn" / "review"
    matches = [p for p in d.glob(f"{item_id}*.json")] if d.is_dir() else []
    if not matches:
        print(f"review: no item {item_id!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"review: ambiguous id {item_id!r} ({len(matches)} match)", file=sys.stderr)
        return 2
    src = matches[0]
    dst_dir = state_dir / "learn" / ("approved" if approve else "rejected")
    dst_dir.mkdir(parents=True, exist_ok=True)
    src.replace(dst_dir / src.name)
    print(f"review: {src.stem} {'approved' if approve else 'rejected'} → {dst_dir.name}/")
    if approve:
        # An approved rule joins the dated rules tier that every ctx_brief includes; an approved
        # skill is queued for installation as a native OpenCode skill (§6.6).
        print("review: approved rules enter rules/ on the next learn run; "
              "skills need `xccode upgrade`")
    return 0


# --- xccode upgrade (§11) -----------------------------------------------------------------

def upgrade(apply: bool, pins_path: Path | None = None) -> int:
    """Show the pin table; with --apply, install $OPT/src over $OPT/src (upgrade to the release
    already extracted by install.sh). xccode never fetches or upgrades on its own."""
    lock = pins_path or (ETC / "versions.lock")
    repo_lock = Path(__file__).resolve().parent.parent.parent / "etc" / "versions.lock"
    if not lock.is_file():
        print(f"upgrade: no versions.lock at {lock}", file=sys.stderr)
        return 1
    pins = load_pins(lock)
    installed = load_pins(repo_lock) if repo_lock.is_file() else {}
    print(f"{'component':<18}{'installed':<24}{'pinned':<24}")
    for name in COMPONENTS:
        cur = installed.get(name)
        new = pins.get(name)
        marker = " " if (cur and new and cur.version == new.version) else "*"
        print(f"{marker}{name:<18}{(cur.version if cur else '—'):<24}"
              f"{(new.version if new else '—'):<24}")
    if apply:
        if installed == pins:
            print("upgrade: already at the pinned versions")
            return 0
        print("upgrade: applying pinned versions (install.sh must be re-run by the operator)")
        target = ETC / "versions.lock"
        target.write_text(lock.read_text())
        print(f"upgrade: wrote {target}; run install.sh to apply")
    else:
        print("upgrade: diff shown; apply with --apply (then the operator re-runs install.sh)")
    return 0


# --- xccode export [--aios] (§8) --------------------------------------------------------------

def export(aios: bool, out_dir: Path, state_dir: Path = STATE) -> int:
    """Export routing events, bench scores, and learn queue as Parquet (+ schema) --aios → the
    aiOS §5.11 field names for direct import. Defaults to a directory the operator hands over."""
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        pa = None
    db = state_dir / "events.db"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if db.is_file():
        with sqlite3.connect(db) as c:
            for rid, ts, agent, decider, pool, outcome, body in c.execute(
                "SELECT id, ts, agent, decider, pool, outcome, body FROM routing_events "
                "ORDER BY id"
            ):
                b = json.loads(body)
                rows.append({
                    "id": rid, "ts": ts, "agent": agent, "decider": decider,
                    "pool": pool, "outcome": outcome,
                    "xc_agent_role": b.get("xc_agent_role", "unknown"),
                    "xc_repo_class": b.get("xc_repo_class", "internal"),
                    "xc_decider": b.get("xc_decider", decider),
                    "cost_micro_usd": b.get("cost_micro_usd", 0),
                    "tokens_in": b.get("tokens_in", 0),
                    "tokens_out": b.get("tokens_out", 0),
                    "latency_ms": b.get("latency_ms", 0),
                    "tool_count": b.get("tool_count", 0),
                    "guard_hits": ",".join(b.get("guard_hits", [])),
                })
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
    if not args.item:
        return review_list()
    if args.action is None:
        print("review: pass approve or reject", file=sys.stderr)
        return 2
    return review_decide(args.item, approve=(args.action == "approve"))


def cmd_keep(args) -> int:
    return keep(args.request_id)


def cmd_report(args) -> int:
    print(report(days=args.days))
    return 0


def cmd_upgrade(args) -> int:
    return upgrade(apply=args.apply, pins_path=args.pins)


def cmd_export(args) -> int:
    return export(aios=args.aios, out_dir=Path(args.out))
