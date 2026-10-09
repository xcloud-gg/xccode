"""OmniRoute provider (XC-CODE-001 §4.5): forward a completion to the private OmniRoute instance.

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
        api_key: str = "",
        client: httpx.Client | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)

    def __call__(
        self,
        pool: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
    ) -> Completion:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
        # The request body carries tools only when the caller sent them (§7 dsh jobs): the
        # plain two-field form keeps text-only clients byte-identical.
        body: dict = {"model": pool, "messages": messages}
        if tools is not None:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        try:
            resp = self._client.post(
                f"{self.base_url}/v1/chat/completions",
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ProviderError(str(e)) from e
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls")
            if tool_calls is not None and not (
                isinstance(tool_calls, list)
                and all(isinstance(tc, dict) for tc in tool_calls)
            ):
                tool_calls = None
            usage = data.get("usage", {})
            return Completion(
                text=message.get("content") or "",
                tokens_in=int(usage.get("prompt_tokens", 0)),
                tokens_out=int(usage.get("completion_tokens", 0)),
                cost_micro_usd=int(usage.get("total_cost", 0)),
                tool_calls=tuple(tool_calls) if tool_calls else None,
            )
        except (KeyError, IndexError, TypeError, AttributeError, ValueError) as e:
            # Malformed upstream payload: never leak the body upstream sent us (O3 review).
            raise ProviderError("malformed provider response") from e
