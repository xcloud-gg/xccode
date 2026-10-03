"""OpenAgentsControl → OpenCode v2 agent-config conversion (XC-CODE-001 §4.2).

OpenAgentsControl (OAC) ships its agents as OpenCode-**v1** files — ``.opencode/agent/**/*.md`` with
``name``/``description``/``mode``/``temperature`` frontmatter and a markdown body. OpenCode **v2**
defines agents in the config ``agent`` key, where the agent's system prompt is the ``prompt`` field.
This module converts the OAC core agents — the OpenCoder orchestration workflow and its validation
and context subagents — into v2 config entries.

Per-agent ``permission`` blocks in the OAC frontmatter are v1-only and are deliberately not
converted: xccode's permission model is config-level (XC-CODE-001 §4.1).
"""

from __future__ import annotations

import re
from pathlib import Path

# The OAC agents xccode adopts: the OpenCoder workflow and the subagents it delegates to.
CORE_AGENTS: list[tuple[str, str]] = [
    ("OpenCoder", ".opencode/agent/core/opencoder.md"),
    ("TestEngineer", ".opencode/agent/subagents/code/test-engineer.md"),
    ("CodeReviewer", ".opencode/agent/subagents/code/reviewer.md"),
    ("BuildAgent", ".opencode/agent/subagents/code/build-agent.md"),
    ("CoderAgent", ".opencode/agent/subagents/code/coder-agent.md"),
    ("ContextScout", ".opencode/agent/subagents/core/contextscout.md"),
    ("TaskManager", ".opencode/agent/subagents/core/task-manager.md"),
]

DEFAULT_MODEL = "xc/auto"

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _field(frontmatter: str, key: str) -> str | None:
    m = re.search(rf"^{key}:\s*(.+)$", frontmatter, re.MULTILINE)
    return m.group(1).strip().strip("\"'") if m else None


def agent_entry(md_path: Path, name: str, model: str = DEFAULT_MODEL) -> dict | None:
    """Convert one OAC ``.md`` agent file into a v2 config entry (or None if not parseable)."""
    text = md_path.read_text()
    m = _FRONTMATTER.match(text)
    if not m:
        return None
    frontmatter, body = m.group(1), m.group(2).strip()
    entry: dict = {
        "model": model,
        "prompt": body,
        "mode": _field(frontmatter, "mode") or "subagent",
    }
    description = _field(frontmatter, "description")
    if description:
        entry["description"] = description
    temperature = _field(frontmatter, "temperature")
    if temperature is not None:
        try:
            entry["temperature"] = float(temperature)
        except ValueError:
            pass
    return entry


def agents_to_config(oac_dir: Path, model: str = DEFAULT_MODEL) -> dict[str, dict]:
    """Convert the OAC core agents under ``oac_dir`` into a v2 ``agent`` config object."""
    out: dict[str, dict] = {}
    for name, rel in CORE_AGENTS:
        md = oac_dir / rel
        if not md.exists():
            continue
        entry = agent_entry(md, name, model=model)
        if entry is not None:
            out[name] = entry
    return out
