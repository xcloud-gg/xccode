from __future__ import annotations

import json
import tempfile
from pathlib import Path

import httpx

from xccode.learn_store import parse_proposals, run, store
from xccode.xcroute.memory import OpenVikingMemory


def _tmp_item(body):
    path = Path(tempfile.mkdtemp()) / "item.json"
    path.write_text(json.dumps(body))
    return path


def _memory(handler):
    transport = httpx.MockTransport(handler)
    return OpenVikingMemory(base_url="http://ov.test", transport=transport)


def test_parse_proposals_from_fenced_json():
    text = '```json\n{"facts": [{"content": "x"}]}\n```'
    assert parse_proposals(text) == {"facts": [{"content": "x"}]}


def test_parse_proposals_from_prose():
    assert parse_proposals('result: {"facts": []}') == {"facts": []}


def test_store_writes_facts_and_pitfalls_to_openviking(tmp_path):
    written = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        written.append((body["uri"], body["tags"]))
        return httpx.Response(200, json={"status": "ok"})

    mem = _memory(handler)
    proposals = {
        "facts": [{"content": "xccode routes via xcroute"}],
        "pitfalls": [{"content": "never rm -rf /"}],
        "preferences": [{"content": "prefer small commits"}],
        "rules": [{"content": "always run tests first"}],
        "skills": [{"name": "karpathy", "content": "think before coding"}],
    }
    summary = store(mem, "xccode", proposals, review_dir=tmp_path / "review")

    assert summary["facts"] == 1
    assert summary["pitfalls"] == 1
    assert summary["preferences"] == 1
    assert summary["review"] == 2  # one rule + one skill

    # facts/pitfalls/preferences went to OpenViking with the observed tag
    assert len(written) == 3
    assert all(tags == ["trust:observed"] for _, tags in written)
    assert any(
        uri.startswith("viking://user/default/memories/projects/xccode/") for uri, _ in written
    )

    # rules + skills went to the review queue (local), not OpenViking
    queued = list((tmp_path / "review").glob("*.json"))
    assert len(queued) == 2


def test_run_parses_and_stores(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    mem = _memory(handler)
    text = '{"facts": [{"content": "a fact"}]}'
    summary = run(mem, "xccode", text, review_dir=tmp_path / "review")
    assert summary["facts"] == 1


def test_promote_rule_writes_rules_tier_with_operator_trust():
    import httpx

    from xccode.learn_store import promote_rule
    from xccode.xcroute.memory import OpenVikingMemory

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["uri"] = request.url.params.get("uri")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok"})

    import json  # noqa: F401  (top-level import in test file keeps this simple)

    mem = OpenVikingMemory(base_url="http://ov.test", transport=httpx.MockTransport(handler))
    f = _tmp_item({"kind": "rule", "content": "always run pytest first", "path": "pytest-first"})
    uri = promote_rule(mem, f)
    assert uri.endswith("/rules/pytest-first")
    assert seen["payload"]["tags"] == ["trust:operator"]


def test_promote_skill_installs_skill_md(tmp_path):
    from xccode.learn_store import promote_skill

    f = _tmp_item({"kind": "skill", "name": "tidy-diff", "content": "---\nname: tidy-diff\n---\n"})
    out = promote_skill(f, tmp_path / "approved")
    assert out.name == "SKILL.md" and out.parent.name == "tidy-diff"
    assert out.read_text().startswith("---")


def test_promote_skill_refuses_pathological_names(tmp_path):
    import pytest

    from xccode.learn_store import promote_skill

    for bad in ("../x", "a/b", ""):
        f = _tmp_item({"kind": "skill", "name": bad, "content": "x"})
        with pytest.raises((ValueError, SystemExit)):
            promote_skill(f, tmp_path)


def test_promote_skill_refuses_symlinked_destination(tmp_path):
    import pytest

    from xccode.learn_store import promote_skill

    target = tmp_path / "approved" / "evil"
    target.mkdir(parents=True)
    (target / "SKILL.md").symlink_to(tmp_path / "victim")
    victim = tmp_path / "victim"
    victim.write_text("keep")
    f = _tmp_item({"kind": "skill", "name": "evil", "content": "payload"})
    with pytest.raises(SystemExit):
        promote_skill(f, tmp_path / "approved")
    assert victim.read_text() == "keep"
