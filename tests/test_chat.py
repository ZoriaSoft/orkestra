"""Tests for the chat-completions transport and JSON-object parsing."""

from __future__ import annotations

import pytest

from orkestra.chat import (
    HttpChatClient,
    chat_completions_url,
    estimate_tokens,
    parse_json_object,
)
from orkestra.errors import ChatError
from orkestra.registry import ResolvedModel
from orkestra.schema import ModelConfig, ProviderConfig, Tier

from .conftest import TEST_KEY_VALUE
from .mock_openai import MockOpenAIServer


def _resolved(provider: ProviderConfig, model_name: str = "m") -> ResolvedModel:
    model = ModelConfig(name=model_name, provider=provider.name, tier=Tier.CHEAP)
    return ResolvedModel(model=model, provider=provider, model_id=model_name, api_key=None)


class TestChatCompletionsUrl:
    @pytest.mark.parametrize(
        ("base", "expected"),
        [
            ("https://h/v1", "https://h/v1/chat/completions"),
            ("https://h", "https://h/v1/chat/completions"),
            ("https://h/api", "https://h/api/v1/chat/completions"),
            ("https://h/v1/", "https://h/v1/chat/completions"),
        ],
    )
    def test_builds_endpoint(self, base: str, expected: str) -> None:
        assert chat_completions_url(base) == expected


class TestHttpChatClient:
    def test_success_returns_content_and_usage(self, mock_server: MockOpenAIServer) -> None:
        mock_server.chat_handler = lambda body: {
            "content": '{"ok": true}',
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }
        provider = ProviderConfig(name="p", base_url=mock_server.v1_url)
        client = HttpChatClient()

        resp = client.complete(
            _resolved(provider),
            [{"role": "user", "content": "hi"}],
            json_mode=True,
            max_tokens=100,
        )

        assert resp.content == '{"ok": true}'
        assert resp.prompt_tokens == 11
        assert resp.completion_tokens == 7

        sent = mock_server.chat_requests[-1]
        assert sent["response_format"] == {"type": "json_object"}
        assert sent["max_tokens"] == 100
        assert sent["messages"][0]["content"] == "hi"

    def test_sends_bearer_header(self, mock_server: MockOpenAIServer) -> None:
        mock_server.require_key = TEST_KEY_VALUE
        mock_server.chat_handler = lambda body: "hello"
        provider = ProviderConfig(
            name="p", base_url=mock_server.v1_url, api_key_env="x"
        )
        resolved = ResolvedModel(
            model=_resolved(provider).model,
            provider=provider,
            model_id="m",
            api_key=TEST_KEY_VALUE,
        )
        resp = HttpChatClient().complete(
            resolved, [{"role": "user", "content": "hi"}]
        )
        assert resp.content == "hello"
        assert mock_server.last_headers().get("Authorization") == f"Bearer {TEST_KEY_VALUE}"

    def test_http_error_raises_chat_error(self, mock_server: MockOpenAIServer) -> None:
        mock_server.mode = "error"
        mock_server.chat_handler = lambda body: "never"
        provider = ProviderConfig(name="p", base_url=mock_server.v1_url)
        with pytest.raises(ChatError, match="HTTP 500"):
            HttpChatClient().complete(
                _resolved(provider), [{"role": "user", "content": "hi"}]
            )

    def test_missing_handler_raises_chat_error(
        self, mock_server: MockOpenAIServer
    ) -> None:
        provider = ProviderConfig(name="p", base_url=mock_server.v1_url)
        with pytest.raises(ChatError, match="HTTP 500"):
            HttpChatClient().complete(
                _resolved(provider), [{"role": "user", "content": "hi"}]
            )

    def test_wrong_shape_raises_chat_error(
        self, mock_server: MockOpenAIServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # simulate a 200 response with an unexpected payload
        import httpx

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self) -> dict:
                return {"unexpected": True}

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
        provider = ProviderConfig(name="p", base_url="http://127.0.0.1:1/v1")
        with pytest.raises(ChatError, match="choices"):
            HttpChatClient().complete(
                _resolved(provider), [{"role": "user", "content": "hi"}]
            )

    def test_network_error_raises_chat_error(self) -> None:
        provider = ProviderConfig(
            name="p", base_url="http://127.0.0.1:1", timeout_seconds=1
        )
        with pytest.raises(ChatError):
            HttpChatClient().complete(
                _resolved(provider), [{"role": "user", "content": "hi"}]
            )


class TestParseJsonObject:
    def test_plain_json(self) -> None:
        assert parse_json_object('{"a": 1}', who="t") == {"a": 1}

    def test_fenced_json(self) -> None:
        assert parse_json_object('```json\n{"a": 1}\n```', who="t") == {"a": 1}

    def test_prose_around_json(self) -> None:
        assert parse_json_object('sure! {"a": 1} done', who="t") == {"a": 1}

    def test_no_json_raises(self) -> None:
        with pytest.raises(ValueError, match="hamal"):
            parse_json_object("no json here", who="hamal")

    def test_non_object_raises(self) -> None:
        with pytest.raises(ValueError, match="object"):
            parse_json_object("[1, 2]", who="sef")


class TestEstimateTokens:
    def test_estimate(self) -> None:
        assert estimate_tokens("") == 1
        assert estimate_tokens("abcd" * 10) == 10
