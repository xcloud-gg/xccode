"""Hermetic tests for xccode.jobs — no git, no systemd, no dsh.

``start``'s subprocess calls are injected; ``status``/``collect`` read only the job dir, so jobs
are simulated by writing the marker files the runner would leave behind (§7).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from xccode import jobs


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def _spawn_ok(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, "", "")


def test_start_creates_worktree_branch_and_meta(tmp_path):
    repo = _git_repo(tmp_path)
    calls: list[list[str]] = []

    def spawn(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    job_id = jobs.start(str(repo), "run the tests", jobs_dir=tmp_path / "jobs", spawn=spawn)
    d = tmp_path / "jobs" / job_id
    meta = json.loads((d / "meta.json").read_text())
    assert meta["branch"] == f"xc/job-{job_id}"
    assert meta["state"] == "running"
    assert (d / "task.txt").read_text() == "run the tests"
    assert (d / "run.sh").exists() and (d / "run.sh").stat().st_mode & 0o111
    # git worktree add is the first spawn call and uses the job branch
    assert calls[0][:5] == ["git", "-C", str(repo), "worktree", "add"]
    assert "-b" in calls[0] and f"xc/job-{job_id}" in calls[0]
    # the second call launches the scope (systemd-run on real hosts, absent in tests)
    assert any("run.sh" in c for c in calls[1])


def test_start_refuses_non_git_repo(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(ValueError, match="not a git repository"):
        jobs.start(str(not_a_repo), "x", jobs_dir=tmp_path / "jobs", spawn=_spawn_ok)


def test_start_refuses_empty_task(tmp_path):
    repo = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="must not be empty"):
        jobs.start(str(repo), "  ", jobs_dir=tmp_path / "jobs", spawn=_spawn_ok)


def _seed_job(tmp_path: Path, task="t") -> tuple[Path, str]:
    repo = _git_repo(tmp_path)
    job_id = jobs.start(str(repo), task, jobs_dir=tmp_path / "jobs", spawn=_spawn_ok)
    return tmp_path / "jobs", job_id


def test_status_running_while_no_done_marker(tmp_path):
    jobs_dir, job_id = _seed_job(tmp_path)
    meta = jobs.status(job_id, jobs_dir, query=lambda unit: True)
    assert meta["state"] == "running"


def test_status_done_when_done_and_rc_zero(tmp_path):
    jobs_dir, job_id = _seed_job(tmp_path)
    d = jobs_dir / job_id
    (d / "rc").write_text("0\n")
    (d / "done").touch()
    meta = jobs.status(job_id, jobs_dir)
    assert meta["state"] == "done" and meta["exit_code"] == "0"


def test_status_failed_when_done_with_nonzero_rc(tmp_path):
    jobs_dir, job_id = _seed_job(tmp_path)
    d = jobs_dir / job_id
    (d / "rc").write_text("1\n")
    (d / "done").touch()
    assert jobs.status(job_id, jobs_dir)["state"] == "failed"


def test_status_failed_when_scope_gone_without_done(tmp_path):
    jobs_dir, job_id = _seed_job(tmp_path)
    meta = jobs.status(job_id, jobs_dir, query=lambda unit: False)
    assert meta["state"] == "failed" and meta["exit_code"] == "?"


def test_status_rejects_unknown_and_bad_ids(tmp_path):
    with pytest.raises(ValueError, match="bad job id"):
        jobs.status("../../etc", tmp_path / "jobs")
    with pytest.raises(ValueError, match="unknown job"):
        jobs.status("20261009T000000-ffffff", tmp_path / "jobs")


def test_collect_refuses_a_running_job(tmp_path):
    jobs_dir, job_id = _seed_job(tmp_path)
    with pytest.raises(ValueError, match="still running"):
        jobs.collect(job_id, jobs_dir)


def test_collect_summarises_branch_diffstat_and_final_answer(tmp_path, monkeypatch):
    jobs_dir, job_id = _seed_job(tmp_path)
    d = jobs_dir / job_id
    (d / "stdout.jsonl").write_text(
        '{"type":"status","phase":"turn_start"}\n'
        '{"type":"final","text":"renamed all the things"}\n'
    )
    (d / "rc").write_text("0\n")
    (d / "done").touch()
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, " src/app.py | 4 ++--\n", ""),
    )
    out = jobs.collect(job_id, jobs_dir)
    assert f"job {job_id}: done (exit 0)" in out
    assert f"branch: xc/job-{job_id}" in out
    assert "src/app.py | 4 ++--" in out
    assert "renamed all the things" in out
