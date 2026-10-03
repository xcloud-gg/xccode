"""`xccode` command line: classify, submit and run P3 plans, and the operator's approver."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from .advisor import AdvisorRecordError, validate_record
from .approver import Approver, ApproverError, check_tty_safe
from .audit import AuditLog
from .doctor import AUDIT_DIR, SYSTEMD_DIR, Host, gather_passwd_group, render, run_checks
from .gate import GateRefused, execute_plan
from .guardrails import apply, load_config, parse_passwd, plan
from .hashing import short
from .store import AdvisorStore, ApprovalStore, PlanStore, StoreError
from .tiers import classify, classify_plan

ETC = Path(os.environ.get("XCCODE_ETC", "/etc/xcloud/xccode"))
STATE = Path(os.environ.get("XCCODE_STATE", "/var/lib/xcloud/xccode"))


def _approver() -> tuple[Approver, PlanStore, AdvisorStore, ApprovalStore, AuditLog]:
    audit = AuditLog(STATE / "audit" / "audit.jsonl")
    plans = PlanStore(STATE / "plans")
    advisors = AdvisorStore(STATE / "advisor")
    approvals = ApprovalStore(STATE / "approvals")
    ap = Approver(
        etc_dir=ETC,
        state_dir=STATE,
        audit=audit,
        plans=plans,
        advisors=advisors,
        approvals=approvals,
    )
    return ap, plans, advisors, approvals, audit


def _card(ap: Approver, plans: PlanStore, advisors: AdvisorStore, p) -> str:
    plan = plans.load(p.plan_hash)
    rec = advisors.load(p.advisor_hash)
    ev = rec["evidence"]
    mark = lambda b: "yes" if b else "NO"  # noqa: E731
    lines = [
        f"P3 REQUEST {p.pending_id}  target: {plan.get('target', '?')}  "
        f"origin: {rec['origin'].get('agent', '?')} ({rec['origin'].get('ref', '?')})",
        f"Why: {plan.get('reason', '?')}",
        f"Advisor estimate: legitimate {rec['legitimacy_pct']}%"
        f"   code quality {rec['quality_score']}/10"
        "   (an estimate; weigh it against the evidence below)",
        f"Evidence: audit log {mark(ev['audit_log'])}"
        f" · signed hand-off {mark(ev['signed_handoff'])}"
        f" · agent's current task matches {mark(ev['task_matches'])}",
        f"Blast radius: {rec['blast_radius']} · Rollback: {rec['rollback']}",
        f"Advisor verdict: {rec['verdict']}",
        "Steps:",
        *[f"  {i + 1}. {s['cmd']}" for i, s in enumerate(plan.get("steps", []))],
        f"plan {short(p.plan_hash)}  advisor-record {short(p.advisor_hash)}",
    ]
    return "\n".join(lines)


def cmd_classify(args) -> int:
    c = classify(" ".join(args.command), remote=args.remote)
    print(f"{c.tier.name}\t{c.rule}")
    return 0


def cmd_submit(args) -> int:
    """Store a plan and advisor record and open a pending request (a proposal, not an approval)."""
    ap, plans, advisors, _, _ = _approver()
    plan = json.loads(Path(args.plan).read_text())
    plan_h = plans.save(plan)
    tier = classify_plan(plan.get("steps", []))
    print(f"plan {plan_h}  tier {tier.tier.name} ({tier.rule})")
    if tier.tier.name != "P3":
        print("not P3: no approval is needed here")
        return 0
    rec = json.loads(Path(args.advisor_record).read_text())
    try:
        validate_record(rec, expected_plan_hash=plan_h)
    except AdvisorRecordError as e:
        print(f"advisor record refused: {e}", file=sys.stderr)
        return 2
    adv_h = advisors.save(rec)
    p = ap.request(plan_h, adv_h)
    print(f"pending {p.pending_id}; the operator approves it with: xccode approve {p.pending_id}")
    return 0


def cmd_run(args) -> int:
    ap, plans, advisors, approvals, audit = _approver()
    ap.clear_abort()

    def runner(step: dict) -> int:
        return subprocess.run(step["cmd"], shell=True, check=False).returncode  # noqa: S602

    try:
        codes = execute_plan(
            args.plan_hash, plans=plans, advisors=advisors, approvals=approvals, audit=audit,
            runner=runner, aborted=ap.aborted,
        )
    except GateRefused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 3
    return 0 if all(c == 0 for c in codes) else 1


def cmd_approve(args) -> int:
    ap, plans, advisors, approvals, audit = _approver()
    target = args.target
    try:
        if target == "abort":
            ap.abort()
            print("abort requested")
            return 0
        if target == "reset":
            ap.reset()
            print("approver reset: hash removed, pending requests and approvals voided")
            return 0
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            audit.append("approver.refused", {"why": "no terminal"})
            print("refusing: the approver needs the operator's terminal", file=sys.stderr)
            return 4
        tty = os.ttyname(0)
        problem = check_tty_safe(os.environ, tty, allow_unsafe=args.allow_unsafe_tty)
        if problem == "exception":
            audit.append("approver.tty-exception", {"tty": tty})
            print("WARNING: unsafe terminal accepted; this is logged", file=sys.stderr)
        elif problem:
            audit.append("approver.refused", {"why": problem})
            print(f"{problem}. Use a text console or SSH from a second device.", file=sys.stderr)
            return 4
        if target == "enroll":
            pw = getpass.getpass("New approver password (not shown): ")
            if getpass.getpass("Again: ") != pw:
                print("passwords differ", file=sys.stderr)
                return 2
            ap.enroll(pw)
            print("enrolled")
            return 0
        pending = ap.current_pending()
        if pending is None or pending.pending_id != target:
            print("no such pending request (it may have expired)", file=sys.stderr)
            return 2
        print(_card(ap, plans, advisors, pending))
        typed = (
            input("Type the pending id: "),
            input("Type the first 8 digits of the plan hash: "),
            input("Type the first 8 digits of the advisor-record hash: "),
        )
        if not ap.check_confirmation(pending, typed):
            print("confirmation does not match; nothing approved", file=sys.stderr)
            return 2
        pw = getpass.getpass("Approver password (not shown): ")
        ap.verify_and_grant(pending, pw, tty_exception=problem == "exception")
        print("approved. The executor waits 30 s; `xccode approve abort` cancels it.")
        return 0
    except (ApproverError, StoreError) as e:
        print(f"{e}", file=sys.stderr)
        return 2


def cmd_guard(args) -> int:
    """Render and (unless --check) write guardrail layers 2, 3 and 4 for existing accounts."""
    etc = Path(os.environ.get("XCCODE_ETC", "/etc/xcloud/xccode"))
    cfg = load_config(args.config)
    accounts = parse_passwd(Path("/etc/passwd").read_text())
    writes = plan(cfg, accounts, etc)
    if args.check:
        for w in writes:
            print(f"# {w.path}")
            print(w.content, end="" if w.content.endswith("\n") else "\n")
        if not writes:
            print("# nothing to write (no aiOS or xcloud account on this host yet)")
        return 0
    written = apply(cfg, accounts, etc)
    for p in written:
        print(f"wrote {p}")
    if not written:
        print("nothing to write (no aiOS or xcloud account on this host yet)")
    return 0


def cmd_doctor(args) -> int:
    """Read-only check of the installation invariants; the fresh-host acceptance gate (B-50)."""
    etc = Path(os.environ.get("XCCODE_ETC", "/etc/xcloud/xccode"))
    state = Path(os.environ.get("XCCODE_STATE", "/var/lib/xcloud/xccode"))
    opt = Path("/opt/xcloud/xccode")
    passwd, gid_to_group, members = gather_passwd_group(
        Path("/etc/passwd").read_text(), Path("/etc/group").read_text()
    )
    candidates = [
        str(etc), str(state), str(opt),
        str(etc / "nft" / "xccode.nft"),
        f"{AUDIT_DIR}/xccode.rules",
    ]
    aios = passwd.get("aios")
    if aios is not None:
        candidates.append(f"{SYSTEMD_DIR}/user@{aios[0]}.service.d/40-xccode-paths.conf")
    existing = frozenset(p for p in candidates if Path(p).exists())
    host = Host(
        passwd=passwd,
        gid_to_group=gid_to_group,
        members=members,
        existing=existing,
        etc_dir=str(etc),
        state_dir=str(state),
        opt_dir=str(opt),
    )
    checks = run_checks(host)
    print(render(checks))
    return 0 if all(c.ok for c in checks) else 1


def cmd_serve(args) -> int:
    """Run the xcroute HTTP server — the `xcroute.service` exec (§4.4)."""
    import uvicorn  # only serve needs the ASGI server

    from .xcroute.http import create_app
    from .xcroute.serve import build_router, load_config

    cfg = load_config(Path(args.config))
    router = build_router(cfg)
    uvicorn.run(create_app(router), host=args.host, port=args.port)
    return 0


def cmd_mcp(args) -> int:
    """Run the xccode MCP server over stdio (§4.11)."""
    from . import xcmcp

    xcmcp.run_stdio()
    return 0


def cmd_oac(args) -> int:
    """Generate the xccode OpenCode profile with OAC agents merged in (§4.2)."""
    import json

    from .oac import agents_to_config

    base = json.loads(Path(args.profile).read_text())
    base["agent"] = agents_to_config(Path(args.oac_dir))
    Path(args.out).write_text(json.dumps(base, indent=2) + "\n")
    return 0


def cmd_bench(args) -> int:
    """Run a routing suite against every pool and print the score table (§4.13)."""
    from .xcbench import bench, load_suite, scores_to_toml
    from .xcroute.provider import OmniRouteProvider
    from .xcroute.serve import load_config

    prompts = load_suite(Path(args.suite))
    pools: dict[str, int] = {}
    if args.config:
        pools = {n: p.est_cost_micro_usd for n, p in load_config(Path(args.config)).pools.items()}
    if not pools:
        print("bench: no pools — pass --config serve.toml (with [pools])", file=sys.stderr)
        return 1
    provider = OmniRouteProvider(base_url=args.base_url, api_key=args.api_key)
    scores = bench(provider, prompts, pools)
    print(scores_to_toml(scores), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="xccode")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("classify", help="print the action tier of a command")
    c.add_argument("--remote", action="store_true")
    c.add_argument("command", nargs="+")
    c.set_defaults(fn=cmd_classify)
    s = sub.add_parser("submit", help="store a plan and open a pending P3 request")
    s.add_argument("plan")
    s.add_argument("--advisor-record", required=True)
    s.set_defaults(fn=cmd_submit)
    r = sub.add_parser("run", help="execute a stored plan through the gate")
    r.add_argument("plan_hash")
    r.set_defaults(fn=cmd_run)
    a = sub.add_parser("approve", help="enroll | <pending-id> | abort | reset")
    a.add_argument("target")
    a.add_argument("--allow-unsafe-tty", action="store_true")
    a.set_defaults(fn=cmd_approve)
    g = sub.add_parser("guard", help="render/write guardrail layers 2, 3 and 4 (§9)")
    g.add_argument("action", choices=["apply"])
    g.add_argument("--config", type=Path, default=ETC / "guardrails.toml")
    g.add_argument("--check", action="store_true", help="preview without writing")
    g.set_defaults(fn=cmd_guard)
    d = sub.add_parser("doctor", help="verify the installation invariants (B-50)")
    d.set_defaults(fn=cmd_doctor)
    srv = sub.add_parser("serve", help="run the xcroute HTTP server on 127.0.0.1:18080 (§4.4)")
    srv.add_argument("--config", type=Path, default=ETC / "serve.toml")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=18080)
    srv.set_defaults(fn=cmd_serve)
    m = sub.add_parser("mcp", help="run the xccode MCP server over stdio (§4.11)")
    m.set_defaults(fn=cmd_mcp)
    o = sub.add_parser("oac", help="generate the OpenCode profile with OAC agents merged (§4.2)")
    o.add_argument("--oac-dir", required=True)
    o.add_argument("--profile", required=True)
    o.add_argument("--out", required=True)
    o.set_defaults(fn=cmd_oac)
    b = sub.add_parser("bench", help="run a routing suite and score every pool (§4.13)")
    b.add_argument("--suite", type=Path, required=True)
    b.add_argument("--config", type=Path, help="serve.toml with [pools] (estimated cost)")
    b.add_argument("--base-url", default="http://127.0.0.1:18128")
    b.add_argument("--api-key", default="")
    b.set_defaults(fn=cmd_bench)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
