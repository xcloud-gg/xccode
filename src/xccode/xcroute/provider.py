"""OmniRoute provider (XC-DES-001 §6.5): forward a completion to the private OmniRoute instance.

OmniRoute is OpenAI-compatible; the pool name is sent as the model so OmniRoute routes within the
pool. No provider key lives here — OmniRoute holds them — so this module only carries the HTTP
call and the `Completion` it produces.
"""

from __future__ import annotations

import httpx

from .router import Completion, ProviderError

DEFAULT_BASE_URL = "http://127.0.0.1:18128"


class OmniRouteProvider:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)

    def __call__(self, pool: str, messages: list[dict]) -> Completion:
        try:
            resp = self._client.post(
                f"{self.base_url}/v1/chat/completions",
                json={"model": pool, "messages": messages},
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ProviderError(str(e)) from e
        choice = data["choices"][0]
        usage = data.get("usage", {})
        return Completion(
            text=choice["message"]["content"],
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            cost_micro_usd=int(usage.get("total_cost", 0)),
        )
