from __future__ import annotations

import httpx

from xccode.xcroute.memory import OpenVikingMemory, _uri


def _client(handler, root="viking://user/default/memories"):
    transport = httpx.MockTransport(handler)
    return OpenVikingMemory(base_url="http://ov.test", root=root, transport=transport), transport


def test_read_maps_tiers_to_endpoints():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "result": request.url.path})

    mem, _ = _client(handler)
    assert mem.read("viking://user/default/memories/x", "L0") == "/api/v1/content/abstract"
    assert mem.read("viking://user/default/memories/x", "L1") == "/api/v1/content/overview"
    assert mem.read("viking://user/default/memories/x", "L2") == "/api/v1/content/read"


def test_read_returns_the_content_string():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "result": "the content"})

    mem, _ = _client(handler)
    assert mem.read("viking://x", "L2") == "the content"


def test_read_returns_empty_when_result_is_not_a_string():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "result": {"unexpected": "dict"}})

    mem, _ = _client(handler)
    assert mem.read("viking://x", "L2") == ""


def test_search_uses_the_memory_search_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/search/search"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "result": {"memories": [{"uri": "viking://x/a"}, {"uri": "viking://x/b"}]},
            },
        )

    mem, _ = _client(handler)
    assert mem.search("q") == [{"uri": "viking://x/a"}, {"uri": "viking://x/b"}]


def test_search_returns_empty_when_no_memories():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "result": {"memories": []}})

    mem, _ = _client(handler)
    assert mem.search("q") == []


def test_brief_aggregates_abstract_overview_rules_pitfalls():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        uri = request.url.params["uri"]
        if path.endswith("/content/abstract"):
            return httpx.Response(200, json={"status": "ok", "result": f"abstract {uri}"})
        if path.endswith("/content/overview"):
            return httpx.Response(200, json={"status": "ok", "result": f"overview {uri}"})
        return httpx.Response(404, json={"status": "error"})

    mem, _ = _client(handler)
    items = mem.brief("xccode")
    tiers = {(i.get("tier"), i.get("kind")) for i in items}
    assert ("L0", None) in tiers and ("L1", None) in tiers  # project abstract + overview
    assert any(i.get("kind") == "rules" for i in items)
    assert any(i.get("kind") == "pitfalls" for i in items)


def test_brief_tolerates_missing_paths():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status": "error"})

    mem, _ = _client(handler)
    assert mem.brief("xccode") == []


def test_uri_join():
    assert _uri("viking://user/default/memories", "projects", "xccode") == (
        "viking://user/default/memories/projects/xccode"
    )
    assert _uri("viking://root/", "") == "viking://root"
