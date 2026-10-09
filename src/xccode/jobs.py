"""Background dsh jobs for xccode-mcp (XC-CODE-001 §7).

OpenCode hands long or parallel work to dsh and keeps going; the result comes back on a branch,
never as edits to the live checkout. ``job_start`` creates an isolated git worktree
(``xc/job-<id>``), launches ``dsh headless`` there inside a ``systemd-run --user`` scope
(MemoryMax=2G, CPUWeight=30), and records the job under
``~/.local/share/xccode/jobs/<id>/`` — ``meta.json``, ``task.txt``, ``job.cordis.yml``,
``run.sh``; the runner leaves ``stdout.jsonl`` (run events), ``stderr.log``, ``rc``, ``done``.

``job_status`` reports running/done/failed from those markers; ``job_collect`` returns the final
answer, the diff stat of the job branch, and the exit code for the operator to merge or discard.

dsh reaches the model only through xcroute (127.0.0.1:18080/v1). The ``dsh-job`` token arrives in
the job scope's environment as ``XCC_JOB_API_KEY`` (systemd ``--setenv``), inherited from the
operator's ``XCC_DSH_TOKEN``/``XCC_TOKEN`` — never written to disk.
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

# The runner template: dsh headless in a bubblewrap sandbox when available (worktree the only rw
# checkout, repo .git rw for the job branch's commits, everything else read-only — §7; the git dir
# must stay writable or git cannot commit the job branch at all), plain otherwise. Writes
# stdout.jsonl (dsh --json events), rc, and the done marker. {dsh} and {repo_git} are rendered per
# job. dsh's own HOME is a per-job dir so its session store never lands in the job branch diff.
RUNNER_TEMPLATE = """#!/bin/sh
# xc job runner — generated per job by xccode.jobs.start; edits are lost.
set -u
cd "$(dirname "$0")"
rc=0
if command -v bwrap >/dev/null 2>&1; then
    # Bind order matters: read-only root first, then the writable overlays on top of it.
    bwrap --ro-bind / / --dev-bind /dev /dev --proc /proc --tmpfs /tmp \\
        --bind "{repo_git}" "{repo_git}" \\
        --bind "$PWD/worktree" "$PWD/worktree" \\
        --bind "$PWD/dsh-home" "$PWD/dsh-home" \\
        --setenv HOME "$PWD/dsh-home" --chdir "$PWD/worktree" \\
        -- {dsh} headless --patch "$PWD/job.cordis.yml" --json - \\
        < task.txt > stdout.jsonl 2> stderr.log || rc=$?
else
    ( cd worktree && {dsh} headless --patch "$PWD/../job.cordis.yml" --json - < ../task.txt ) \\
        > stdout.jsonl 2> stderr.log || rc=$?
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


def _is_git_repo(repo: Path) -> bool:
    return (repo / ".git").exists()


def start(
    repo: str,
    task: str,
    jobs_dir: Path = JOBS_DIR,
    spawn: Callable = subprocess.run,
) -> str:
    """Create the worktree and launch `dsh headless` scoped by systemd-run; return the job id."""
    repo_path = Path(repo).expanduser().resolve()
    if not repo_path.is_dir() or not _is_git_repo(repo_path):
        raise ValueError(f"job repo is not a git repository: {repo}")
    if not task.strip():
        raise ValueError("job task must not be empty")
    job_id = time.strftime("%Y%m%dT%H%M%S") + f"-{secrets.token_hex(3)}"
    branch = f"xc/job-{job_id}"
    d = jobs_dir / job_id
    d.mkdir(parents=True)
    (d / "dsh-home").mkdir()  # dsh's session store inside the sandbox; never in the branch diff
    (d / "task.txt").write_text(task)
    worktree = d / "worktree"
    spawn(
        ["git", "-C", str(repo_path), "worktree", "add", str(worktree), "-b", branch],
        check=True, capture_output=True, text=True,
    )
    # A per-job copy of the job composition: the runner stays valid if the system copy changes.
    if Path(JOB_CORDIS).is_file():
        shutil.copy(JOB_CORDIS, d / "job.cordis.yml")
    else:
        (d / "job.cordis.yml").write_text("")  # patched out only for tests without an install
    (d / "meta.json").write_text(json.dumps({
        "job_id": job_id,
        "repo": str(repo_path),
        "branch": branch,
        "worktree": str(worktree),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "task": task,
        "state": "running",
    }, indent=2) + "\n")
    runner = d / "run.sh"
    runner.write_text(
        RUNNER_TEMPLATE.replace("{dsh}", DSH).replace("{repo_git}", str(repo_path / ".git"))
    )
    runner.chmod(0o755)
    token = os.environ.get("XCC_DSH_TOKEN") or os.environ.get("XCC_TOKEN", "")
    env = dict(os.environ)
    env["PATH"] = f"{NODE_BIN}:{env.get('PATH', '/usr/bin')}"
    # Commits on the job branch are authored by the job runner, not the operator (a background
    # job must never borrow the operator's git identity, which may be unset on a fresh install).
    runner_env = {
        "XCC_JOB_API_KEY": token,
        "PATH": f"{NODE_BIN}:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/"),
        "GIT_AUTHOR_NAME": "xccode job",
        "GIT_AUTHOR_EMAIL": "xccode-jobs@localhost",
        "GIT_COMMITTER_NAME": "xccode job",
        "GIT_COMMITTER_EMAIL": "xccode-jobs@localhost",
    }
    sysd = shutil.which("systemd-run")
    if sysd:
        setenv = [e for kv in runner_env.items() for e in ("--setenv", "=".join(kv))]
        spawn(
            [sysd, "--user", "--scope", "-p", "MemoryMax=2G", "-p", "CPUWeight=30",
             "--collect", "--unit", f"xc-job-{job_id}",
             *setenv,
             str(d / "run.sh")],
            check=True, env=env,
        )
    else:  # hosts without a user manager (test hosts): run detached with the same env
        subprocess.Popen(
            [str(d / "run.sh")], cwd=d, start_new_session=True, env={**env, **runner_env},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return job_id


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

    ``query(unit)`` (optional) checks whether the systemd scope is still active; a gone scope
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
    """Summary + diff stat of the job branch; refuses while the job is still running."""
    meta = status(job_id, jobs_dir)
    if meta["state"] == "running":
        raise ValueError(f"job {job_id!r} is still running")
    d = meta["_dir"]
    diffstat = subprocess.run(
        ["git", "-C", meta["repo"], "diff", "--stat", "HEAD", meta["branch"]],
        capture_output=True, text=True,
    ).stdout.strip()
    # Uncommitted work in the job worktree is still operator-visible output (dsh's fs tools write
    # without committing; dsh's commit step may fail mid-job). Report it next to the branch diff.
    dirty = subprocess.run(
        ["git", "-C", meta["worktree"], "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout.strip()
    summary = _final_text(d / "stdout.jsonl")
    lines = [
        f"job {job_id}: {meta['state']} (exit {meta.get('exit_code', '?')})",
        f"branch: {meta['branch']}  (merge or discard)",
        "",
        "diff --stat:",
        diffstat or "(no committed changes)",
    ]
    if dirty:
        lines += ["", "worktree (uncommitted):", dirty]
    lines += ["", "final answer:", summary or "(empty)"]
    return "\n".join(lines) + "\n"
