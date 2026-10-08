"""Learner store: turn Hermes's proposals into OpenViking memory and a review queue (§6.5).

Facts, preferences and pitfalls enter OpenViking as *observed* (``trust:observed``). Rules and
skill drafts do not enter memory directly — they land in a review queue for the operator's
``xccode review`` before they are promoted. Hermes proposes a JSON object; this module parses it
(tolerating a markdown fence), then writes each item through OpenViking's content API or the local
review directory.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .hashing import sha256_hex
from .xcroute.memory import OpenVikingMemory

REVIEW_DIR = Path("/var/lib/xcloud/xccode/learn/review")


def parse_proposals(text: str) -> dict:
    """Pull the first JSON object out of Hermes's reply (it may wrap it in a markdown fence)."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def _path_for(entry: dict, kind: str) -> str:
    return str(entry.get("path") or sha256_hex(str(entry.get("content", "")).encode())[:12])


def store(
    memory: OpenVikingMemory,
    repo: str,
    proposals: dict,
    review_dir: Path = REVIEW_DIR,
) -> dict:
    """Write facts/preferences/pitfalls to OpenViking and rules/skills to the review queue."""
    out = {"facts": 0, "pitfalls": 0, "preferences": 0, "review": 0}

    for entry in proposals.get("facts", []):
        uri = f"{memory.root}/projects/{repo}/{_path_for(entry, 'fact').lstrip('/')}"
        memory.write(uri, str(entry["content"]), tags=["trust:observed"])
        out["facts"] += 1

    for entry in proposals.get("pitfalls", []):
        uri = f"{memory.root}/projects/{repo}/pitfalls/{_path_for(entry, 'pitfall').lstrip('/')}"
        memory.write(uri, str(entry["content"]), tags=["trust:observed"])
        out["pitfalls"] += 1

    for entry in proposals.get("preferences", []):
        uri = f"{memory.root}/memory/{_path_for(entry, 'preference').lstrip('/')}"
        memory.write(uri, str(entry["content"]), tags=["trust:observed"])
        out["preferences"] += 1

    review_dir.mkdir(parents=True, exist_ok=True)
    for entry in proposals.get("rules", []) + proposals.get("skills", []):
        body = {"kind": "rule" if "content" in entry and "name" not in entry else "skill", **entry}
        path = review_dir / f"{sha256_hex(json.dumps(body, sort_keys=True).encode())[:16]}.json"
        if not path.exists():
            path.write_text(json.dumps(body, indent=2) + "\n")
        out["review"] += 1

    return out


def run(memory: OpenVikingMemory, repo: str, text: str, review_dir: Path = REVIEW_DIR) -> dict:
    """Parse Hermes's reply and store it. Returns the summary dict."""
    return store(memory, repo, parse_proposals(text), review_dir)
