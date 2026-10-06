from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from conformance.maas import MaaSClient


def test_create_api_key_sends_bearer_user_token_and_name():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"key": "sk-oai-test"})

    with closing(MaaSClient("http://maas.example", transport=httpx.MockTransport(handler))) as client:
        response = client.create_api_key("k8s-user-token", "llm-d-e2e")

    assert response.status_code == 201
    assert requests[0].url.path == "/v1/api-keys"
    assert requests[0].headers["authorization"] == "Bearer k8s-user-token"
    assert requests[0].read() == b'{"name":"llm-d-e2e","expiresIn":"7d"}'


def test_chat_completion_uses_api_key_bearer_and_returns_error_responses():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, json={"error": "rate limit"})

    with closing(MaaSClient("http://maas.example", transport=httpx.MockTransport(handler))) as client:
        response = client.chat_completion("publishers/llm/models/qwen", "hello", api_key="sk-oai-test")

    assert response.status_code == 429
    assert requests[0].url.path == "/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer sk-oai-test"
    assert (
        requests[0].read() == b'{"model":"publishers/llm/models/qwen","messages":[{"role":"user","content":"hello"}]}'
    )


def test_chat_completion_can_send_unauthenticated_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(401)

    with closing(MaaSClient("http://maas.example", transport=httpx.MockTransport(handler))) as client:
        response = client.chat_completion("model-alias", "hello")

    assert response.status_code == 401
    assert "authorization" not in requests[0].headers
