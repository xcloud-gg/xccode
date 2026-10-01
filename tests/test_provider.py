import json

import httpx
import pytest

from xccode.xcroute.provider import OmniRouteProvider
from xccode.xcroute.router import ProviderError


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_posts_pool_as_model_and_returns_completion():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi from pool"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
        )

    p = OmniRouteProvider("http://127.0.0.1:18128", client=_client(handler))
    c = p("coding-strong", [{"role": "user", "content": "hi"}])
    assert c.text == "hi from pool"
    assert c.tokens_in == 1
    assert c.tokens_out == 2
    assert seen["url"] == "http://127.0.0.1:18128/v1/chat/completions"
    assert seen["body"]["model"] == "coding-strong"
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]


def test_non_200_raises_provider_error():
    def handler(request):
        return httpx.Response(500, text="boom")

    p = OmniRouteProvider("http://127.0.0.1:18128", client=_client(handler))
    with pytest.raises(ProviderError):
        p("fast", [])


def test_network_failure_raises_provider_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    p = OmniRouteProvider("http://127.0.0.1:18128", client=_client(handler))
    with pytest.raises(ProviderError):
        p("fast", [])
