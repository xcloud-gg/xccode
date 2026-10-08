from __future__ import annotations

import json

import httpx
import pytest

from xccode.advisor import REQUIRED_POOL
from xccode.cloud_advisor import AdvisorConfig, CloudAdvisor, _extract_json
from xccode.hashing import plan_hash


def _review_response() -> dict:
    return {
        "legitimacy_pct": 82,
        "quality_score": 7,
        "evidence": {"audit_log": True, "signed_handoff": True, "task_matches": False},
        "blast_radius": "removes a backup",
        "rollback": "yes, restore from offsite",
        "verdict": "legitimate but review the retention",
    }


def _client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def test_extract_json_from_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_from_fenced():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_from_prose_wrapped():
    assert _extract_json('here is the answer: {"a": 1} done') == {"a": 1}


def test_review_sanitises_and_builds_record(tmp_path):
    plan = {
        "target": "thor",
        "reason": "rotate keys",
        "steps": [{"cmd": "gpg --delete-secret-key", "remote": False}],
    }
    # a canary secret + IP that must be redacted/pseudonymised before it leaves
    plan["note"] = "token sk-ant-" + "a" * 40 + " from 192.0.2.1"

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        assert "sk-ant-" not in request.content.decode()  # secret redacted
        assert "192.0.2.1" not in request.content.decode()  # IP pseudonymised
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": json.dumps(_review_response())}]},
        )

    advisor = CloudAdvisor(
        cfg=AdvisorConfig(api_key="test-key"),
        advisor_dir=tmp_path / "advisor",
        client=_client(handler),
    )
    ph = plan_hash(plan)
    rec = advisor.review(plan, ph, {"agent": "test", "ref": "x"})

    assert rec["plan_hash"] == ph
    assert rec["pool"] == REQUIRED_POOL
    assert rec["legitimacy_pct"] == 82
    assert rec["evidence"]["task_matches"] is False
    assert rec["sent_payload_hash"]
    # the exact sanitised payload is stored for audit
    assert list((tmp_path / "advisor").glob("*.sent.json"))


def test_review_requires_an_api_key(tmp_path):
    advisor = CloudAdvisor(cfg=AdvisorConfig(api_key=""), advisor_dir=tmp_path)
    with pytest.raises(RuntimeError):
        advisor.review({"steps": []}, "hash")


def test_model_is_the_reserved_opus(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["model"] = json.loads(request.content)["model"]
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": json.dumps(_review_response())}]}
        )

    advisor = CloudAdvisor(
        cfg=AdvisorConfig(model="claude-opus-5-5", api_key="k"),
        advisor_dir=tmp_path,
        client=_client(handler),
    )
    advisor.review({"steps": []}, "h")
    assert captured["model"] == "claude-opus-5-5"
