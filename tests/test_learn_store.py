from __future__ import annotations

import json

import httpx

from xccode.learn_store import parse_proposals, run, store
from xccode.xcroute.memory import OpenVikingMemory


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
