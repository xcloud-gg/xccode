"""Background dsh jobs for xccode-mcp (XC-CODE-001 §7).

OpenCode hands long or parallel work to dsh and keeps going; the result comes back on a branch,
never as edits to the live checkout. ``job_start`` creates an isolated git worktree
(``xc/job-<id>``) and launches a fixed runner as a transient user service (``systemd-run --user``,
MemoryMax=2G, CPUWeight=30), then returns the job id immediately — the MCP call never blocks.

The runner confines dsh in bubblewrap (advisor C4 hardening):
- the operator's ``$HOME`` is a tmpfs — serve passwords, tokens, keys are unreadable inside;
- the repository checkout is read-only and its ``.git`` is NOT writable during the dsh step,
  so a (compromised or prompt-injected) job cannot plant hooks, fsmonitor, or filter config;
- fresh PID/IPC namespaces, minimal /dev, host /tmp hidden;
- after dsh exits, a separate short sandbox commits whatever the job left — with hooks and
  fsmonitor disabled (the commit is the one moment the job branch's git dir is writable).

dsh reaches the model only through xcroute (127.0.0.1:18080/v1) with the ``dsh-job`` token,
passed to the unit as ``XCC_JOB_API_KEY`` (from the operator's ``XCC_DSH_TOKEN``). Job state
(meta.json/task.txt/stdout.jsonl/rc/done) lives in ``~/.local/share/xccode/jobs/<id>/``.
``job_status`` reports running/done/failed; ``job_collect`` returns the final answer, the
merge-base diff stat of the branch, and the exit code for the operator to merge or discard.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

JOBS_DIR = Path(
    os.environ.get("XCCODE_JOBS", Path.home() / ".local" / "share" / "xccode" / "jobs")
)
# Pinned Node 24 prefix that carries dsh; mirrored in install.sh step_dsh (XC-CODE-001 §4.12).
NODE_BIN = os.environ.get("XCCODE_NODE_BIN", "/opt/xcloud/xccode/node-v24.14.1-linux-x64/bin")
DSH = os.environ.get("XCCODE_DSH", f"{NODE_BIN}/dsh")
# The headless job composition (telemetry off, model → xcroute); install.sh step_dsh writes it.
JOB_CORDIS = os.environ.get("XCCODE_JOB_CORDIS", "/etc/xcloud/xccode/dsh/job.cordis.yml")

_JOB_ID = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$")
# Repo paths must be plain (advisor C2): no whitespace, $, quotes, or backticks can survive into
# a shell context, so reject those chars outright on top of passing values by environment.
_REPO_PATH = re.compile(r"^[A-Za-z0-9._/+=@,-]+$")

# Git invocations made by THIS PROCESS or the runner must never trigger config/hooks the repo
# (or a job) controls: hooksPath=/dev/null, fsmonitor=false (advisor C4).
GIT_SAFE = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")

# Fixed runner: every per-job value arrives via XCC_* environment variables (nothing is
# interpolated into shell text — advisor C2). Two bubblewrap sandboxes: dsh (no writable .git),
# then a short commit step with the job branch's git dir writable and hooks/fsmonitor disabled.
RUNNER = r"""#!/bin/sh
# xc job runner — fixed file; values arrive as XCC_* env (set by the transient unit).
set -u
cd "$(dirname "$0")" || exit 1
JOB_DIR="$PWD"
rc=0

bwrap --ro-bind / / --tmpfs "$HOME" --tmpfs /tmp \
    --ro-bind "$XCC_REPO" "$XCC_REPO" \
    --bind "$JOB_DIR/worktree" "$JOB_DIR/worktree" \
    --bind "$JOB_DIR/dsh-home" "$JOB_DIR/dsh-home" \
    --dev /dev --unshare-pid --proc /proc --unshare-ipc --new-session --die-with-parent \
    --setenv HOME "$JOB_DIR/dsh-home" --chdir "$JOB_DIR/worktree" \
    -- "$XCC_DSH" headless --patch "$JOB_DIR/job.cordis.yml" --json - \
    < task.txt > stdout.jsonl 2> stderr.log || rc=$?

if [ -n "$(git -C worktree -c core.fsmonitor=false -c core.hooksPath=/dev/null \
            status --porcelain 2>/dev/null)" ]; then
    bwrap --ro-bind / / --tmpfs "$HOME" --tmpfs /tmp \
        --ro-bind "$XCC_REPO" "$XCC_REPO" \
        --bind "$XCC_REPO_GIT" "$XCC_REPO_GIT" \
        --bind "$JOB_DIR/worktree" "$JOB_DIR/worktree" \
        --bind "$JOB_DIR/dsh-home" "$JOB_DIR/dsh-home" \
        --dev /dev --unshare-pid --proc /proc --unshare-ipc --new-session --die-with-parent \
        --setenv HOME "$JOB_DIR/dsh-home" --chdir "$JOB_DIR/worktree" \
        -- /bin/sh -c 'git -c core.fsmonitor=false -c core.hooksPath=/dev/null add -A && \
                       git -c core.fsmonitor=false -c core.hooksPath=/dev/null \
                           commit --no-verify -m "xccode job output"' \
        >> stdout.jsonl 2>> stderr.log || true
fi

echo "$rc" > rc
touch done
exit 0
"""


def _job_dir(job_id: str, jobs_dir: Path = JOBS_DIR) -> Path:
    if not _JOB_ID.match(job_id):
        raise ValueError(f"bad job id: {job_id!r}")
    d = jobs_dir / job_id
    if not str(d).startswith(str(jobs_dir)) or ".." in str(d):
        raise ValueError(f"bad job id: {job_id!r}")
    return d


def _git_common_dir(repo: Path) -> Path:
    """The repo's real git dir (a worktree input's .git file points at the main one)."""
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    p = Path(out)
    return (repo / p).resolve() if not p.is_absolute() else p


def start(
    repo: str,
    task: str,
    jobs_dir: Path = JOBS_DIR,
    spawn: Callable = subprocess.run,
) -> str:
    """Create the worktree and launch the confined runner as a transient user service.

    Returns the job id immediately. Raises ValueError for a bad repo/task and bubbles
    CalledProcessError for launch failures; a failed launch removes the worktree and branch.
    """
    repo_path = Path(repo).expanduser().resolve()
    if not repo_path.is_dir() or not (repo_path / ".git").exists():
        raise ValueError(f"job repo is not a git repository: {repo}")
    if not task.strip():
        raise ValueError("job task must not be empty")
    if not _REPO_PATH.match(str(repo_path)):
        raise ValueError(f"job repo path has unsafe characters: {repo!r}")
    if shutil.which("bwrap") is None and not os.environ.get("XCCODE_JOBS_UNSAFE_ALLOW"):
        raise ValueError("bubblewrap is required for jobs (no unconfined fallback)")
    token = os.environ.get("XCC_DSH_TOKEN", "")
    if not token:
        raise ValueError("XCC_DSH_TOKEN is not set (install.sh step_xcc_env writes it)")

    job_id = time.strftime("%Y%m%dT%H%M%S") + f"-{secrets.token_hex(3)}"
    branch = f"xc/job-{job_id}"
    d = jobs_dir / job_id
    d.mkdir(parents=True, mode=0o700)
    worktree = d / "worktree"
    d.mkdir(mode=0o700, exist_ok=True)
    (d / "dsh-home").mkdir(mode=0o700)

    def _write(path: Path, text: str, mode: int = 0o600) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.write(fd, text.encode())
        finally:
            os.close(fd)

    spawn(
        ["git", "-C", str(repo_path), *GIT_SAFE, "worktree", "add", str(worktree), "-b", branch],
        check=True, capture_output=True, text=True,
    )
    try:
        git_common = _git_common_dir(repo_path)
        if not _REPO_PATH.match(str(git_common)):
            raise ValueError(f"repo git dir has unsafe characters: {git_common!r}")
        _write(d / "task.txt", task)
        if Path(JOB_CORDIS).is_file():
            shutil.copy(JOB_CORDIS, d / "job.cordis.yml")
            (d / "job.cordis.yml").chmod(0o600)
        else:
            _write(d / "job.cordis.yml", "")  # patched out only for test hosts
        _write(d / "meta.json", json.dumps({
            "job_id": job_id,
            "repo": str(repo_path),
            "branch": branch,
            "worktree": str(worktree),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "task": task,
            "state": "running",
        }, indent=2) + "\n")
        runner = d / "run.sh"
        _write(runner, RUNNER, 0o700)
        env_pairs = {
            "XCC_DIR": str(d),
            "XCC_REPO": str(repo_path),
            "XCC_REPO_GIT": str(git_common),
            "XCC_DSH": DSH,
            "XCC_JOB_API_KEY": token,
            "HOME": os.environ.get("HOME", "/home/marius"),
            "PATH": f"{NODE_BIN}:/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "xccode job",
            "GIT_AUTHOR_EMAIL": "xccode-jobs@localhost",
            "GIT_COMMITTER_NAME": "xccode job",
            "GIT_COMMITTER_EMAIL": "xccode-jobs@localhost",
        }
        sysd = shutil.which("systemd-run")
        if sysd:
            setenv = [e for kv in env_pairs.items() for e in ("--setenv", "=".join(kv))]
            # A transient user SERVICE (not --scope): returns at once, starts with ONLY the
            # listed environment (no caller-env leak), output goes to the journal — the MCP
            # stdio channel stays clean (advisor C3).
            spawn(
                [sysd, "--user", "--quiet", "--collect", "--unit", f"xc-job-{job_id}",
                 "-p", "MemoryMax=2G", "-p", "CPUWeight=30",
                 *setenv, str(runner)],
                check=True, capture_output=True, text=True,
                env={"PATH": os.environ.get("PATH", "/usr/bin")},
            )
        else:  # hosts without a user manager (test hosts): run detached with the same minimal env
            subprocess.Popen(
                [str(runner)], cwd=d, start_new_session=True, env=env_pairs,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return job_id
    except Exception:
        _cleanup(repo_path, worktree, branch, d)
        raise


def _cleanup(repo: Path, worktree: Path, branch: str, job_dir: Path) -> None:
    """Best-effort removal of a failed launch's worktree, branch, and job dir."""
    subprocess.run(
        ["git", "-C", str(repo), *GIT_SAFE, "worktree", "remove", "--force", str(worktree)],
        capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), *GIT_SAFE, "branch", "-D", branch],
        capture_output=True, text=True,
    )
    shutil.rmtree(job_dir, ignore_errors=True)


def _load(job_id: str, jobs_dir: Path) -> dict:
    d = _job_dir(job_id, jobs_dir)
    meta_p = d / "meta.json"
    if not meta_p.is_file():
        raise ValueError(f"unknown job: {job_id!r}")
    return {**json.loads(meta_p.read_text()), "_dir": d}


def status(
    job_id: str,
    jobs_dir: Path = JOBS_DIR,
    query: Callable[[str], bool] | None = None,
) -> dict:
    """Job state: running until the `done` marker; then done (rc 0) or failed.

    ``query(unit)`` (optional) checks whether the systemd unit is still active; a gone unit
    without a `done` marker means the runner died, so the job is failed.
    """
    meta = _load(job_id, jobs_dir)
    d = meta["_dir"]
    if (d / "done").exists():
        rc = (d / "rc").read_text().strip() if (d / "rc").exists() else "?"
        meta["state"] = "done" if rc == "0" else "failed"
        meta["exit_code"] = rc
    elif query is not None and query(f"xc-job-{job_id}") is False:
        meta["state"] = "failed"
        meta["exit_code"] = "?"
    return meta


def _final_text(stdout_jsonl: Path) -> str:
    """The job's final answer: the last `final` event in dsh's JSONL stream."""
    final = ""
    try:
        with open(stdout_jsonl, errors="replace") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "final":
                    final = event.get("text", "")
    except OSError:
        pass
    return final


def collect(job_id: str, jobs_dir: Path = JOBS_DIR) -> str:
    """Summary + merge-base diff stat of the job branch; refuses while the job is running."""
    meta = status(job_id, jobs_dir)
    if meta["state"] == "running":
        raise ValueError(f"job {job_id!r} is still running")
    d = meta["_dir"]
    diffstat = subprocess.run(
        ["git", "-C", meta["repo"], *GIT_SAFE, "diff", "--stat", f"HEAD...{meta['branch']}"],
        capture_output=True, text=True,
    ).stdout.strip()
    # Uncommitted work in the job worktree is still operator-visible output (the runner's commit
    # step can be skipped on a failed dsh). Report it next to the branch diff.
    dirty = subprocess.run(
        ["git", "-C", meta["worktree"], *GIT_SAFE, "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout.strip()
    summary = _final_text(d / "stdout.jsonl")
    lines = [
        f"job {job_id}: {meta['state']} (exit {meta.get('exit_code', '?')})",
        f"branch: {meta['branch']}  (merge or discard)",
        "",
        "diff --stat (merge-base):",
        diffstat or "(no committed changes)",
    ]
    if dirty:
        lines += ["", "worktree (uncommitted):", dirty]
    lines += ["", "final answer:", summary or "(empty)"]
    return "\n".join(lines) + "\n"
