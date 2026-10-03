"""Tests for the OpenAgentsControl → OpenCode v2 agent-config conversion (XC-CODE-001 §4.2)."""

from xccode.oac import agent_entry, agents_to_config


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_agent_entry_converts_frontmatter_and_body(tmp_path):
    md = tmp_path / "opencoder.md"
    md.write_text(
        "---\n"
        "name: OpenCoder\n"
        'description: "Orchestration agent"\n'
        "mode: primary\n"
        "temperature: 0.1\n"
        "permission:\n"
        '  bash:\n    "sudo *": "deny"\n'
        "---\n"
        "# Development Agent\n"
        "Always use ContextScout for discovery.\n"
    )
    entry = agent_entry(md, "OpenCoder")
    assert entry["mode"] == "primary"
    assert entry["description"] == "Orchestration agent"
    assert entry["temperature"] == 0.1
    assert entry["model"] == "xc/auto"
    assert "ContextScout" in entry["prompt"]
    assert "permission" not in entry  # v1 per-agent permission is dropped


def test_agents_to_config_collects_core_agents(tmp_path):
    oac = tmp_path / "oac"
    a = oac / ".opencode/agent/subagents/code"
    c = oac / ".opencode/agent/subagents/core"
    _write(oac / ".opencode/agent/core/opencoder.md", "---\nmode: primary\n---\nbody-opencoder")
    _write(a / "test-engineer.md", "---\nmode: subagent\n---\nbody-tester")
    _write(a / "reviewer.md", "---\nmode: subagent\n---\nbody-reviewer")
    _write(a / "build-agent.md", "---\nmode: subagent\n---\nbody-builder")
    _write(a / "coder-agent.md", "---\nmode: subagent\n---\nbody-coder")
    _write(c / "contextscout.md", "---\nmode: subagent\n---\nbody-scout")
    _write(c / "task-manager.md", "---\nmode: subagent\n---\nbody-tasker")

    cfg = agents_to_config(oac)
    assert set(cfg) == {
        "OpenCoder",
        "TestEngineer",
        "CodeReviewer",
        "BuildAgent",
        "CoderAgent",
        "ContextScout",
        "TaskManager",
    }
    assert cfg["OpenCoder"]["mode"] == "primary"
    assert cfg["TestEngineer"]["mode"] == "subagent"
    assert cfg["OpenCoder"]["prompt"] == "body-opencoder"


def test_missing_agent_files_are_skipped(tmp_path):
    assert agents_to_config(tmp_path) == {}
