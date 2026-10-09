"""Read-only OpenViking client for xcroute's ``/ctx`` door (XC-CODE-001 §4.7, §4.11).

OpenCode reaches project memory only through this narrow read-only door — it never gets
OpenViking's write API. ``ctx_read`` (by tier), ``ctx_search`` (semantic recall), and ``ctx_brief``
(repo abstract + overview + dated rules + pitfalls) map onto OpenViking's content and search
endpoints. Long-term memory lives under ``viking://user/default/memories/`` — OpenViking's native
scheme, not the spec §4.7 ``viking://shared/``/``nodes/`` names (see DECISIONS for the drift).
"""

from __future__ import annotations

import httpx

OPENVIKING_URL = "http://127.0.0.1:18180"
MEMORY_ROOT = "viking://user/default/memories"

# The memory layout under the root (XC-CODE-001 §6): projects/<repo>/ (L0 abstract · L1 overview ·
# L2 notes), rules/ (dated rules), pitfalls/ (pitfalls).
PROJECTS = "projects"
RULES = "rules"
PITFALLS = "pitfalls"


def _uri(root: str, *parts: str) -> str:
    joined = "/".join([root.rstrip("/")] + [p.strip("/") for p in parts if p.strip("/")])
    return joined


class OpenVikingMemory:
    """A minimal, read-only view of OpenViking for the /ctx door. No writes."""

    def __init__(
        self,
        base_url: str = OPENVIKING_URL,
        root: str = MEMORY_ROOT,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.root = root
        self._transport = transport  # injectable for tests

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=self._transport, timeout=10.0)

    def _get(self, endpoint: str, **params) -> dict:
        with self._client() as c:
            r = c.get(endpoint, params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, endpoint: str, body: dict) -> dict:
        with self._client() as c:
            r = c.post(endpoint, json=body)
        r.raise_for_status()
        return r.json()

    def read(self, uri: str, tier: str = "L1") -> str:
        """Read one memory entry by tier: L0 abstract / L1 overview / L2 full content."""
        if tier == "L0":
            data = self._get("/api/v1/content/abstract", uri=uri)
        elif tier == "L1":
            data = self._get("/api/v1/content/overview", uri=uri)
        else:
            data = self._get("/api/v1/content/read", uri=uri)
        result = data.get("result")
        return result if isinstance(result, str) else ""

    def write(self, uri: str, content: str, tags: list[str] | None = None) -> dict:
        """Write (replace) one memory entry. Learner store only, never the read-only /ctx."""
        body: dict = {"uri": uri, "content": content}
        if tags:
            body["tags"] = tags
        return self._post("/api/v1/content/write", body)

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Semantic search over the memory content tree (§4.11).

        Uses ``/api/v1/search/search`` — the vector search over the memories tree that content
        writes are embedded into. (``/api/v1/search/recall`` searches only the VLM-extracted
        entities/events/preferences store, which the observer pipeline populates separately and
        store-learn does not write to.) Each entry is ``{uri, abstract, score, tags, level}``.
        """
        data = self._post("/api/v1/search/search", {"query": query, "limit": limit})
        entries = data.get("result", {}).get("memories", [])
        return entries if isinstance(entries, list) else []

    def brief(self, repo: str) -> list[dict]:
        """The repository brief: abstract + overview + dated rules + pitfalls (spec §4.11)."""
        items: list[dict] = []
        project = _uri(self.root, PROJECTS, repo)
        for tier, uri in (("L0", project), ("L1", project)):
            try:
                items.append({"uri": uri, "tier": tier, "content": self.read(uri, tier)})
            except (httpx.HTTPError, KeyError):
                continue
        for kind in (RULES, PITFALLS):
            uri = _uri(self.root, kind)
            try:
                items.append({"uri": uri, "kind": kind, "content": self.read(uri, "L1")})
            except (httpx.HTTPError, KeyError):
                continue
        return items
