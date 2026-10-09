"""Integration tests for the OpenAI-compatible probe client.

Runs against a real local mock server — no library internals are patched.
"""

from __future__ import annotations

from orkestra.client import models_url, probe_provider
from orkestra.schema import ProviderConfig

from .conftest import TEST_KEY_VALUE, unused_port
from .mock_openai import MockOpenAIServer


class TestModelsUrl:
    def test_v1_suffix_preserved(self) -> None:
        assert models_url("https://h/v1") == "https://h/v1/models"

    def test_v1_appended(self) -> None:
        assert models_url("https://h") == "https://h/v1/models"

    def test_custom_path(self) -> None:
        assert models_url("https://h/api") == "https://h/api/v1/models"

    def test_trailing_slash(self) -> None:
        assert models_url("https://h/v1/") == "https://h/v1/models"


class TestProbe:
    def test_ok_lists_models(
        self, mock_server: MockOpenAIServer, mock_provider: ProviderConfig
    ) -> None:
        mock_server.require_key = TEST_KEY_VALUE
        result = probe_provider(mock_provider, TEST_KEY_VALUE)
        assert result.ok
        assert result.status_code == 200
        assert result.latency_ms >= 0
        assert result.model_ids == sorted(mock_server.models)

    def test_bearer_header_sent(
        self, mock_server: MockOpenAIServer, mock_provider: ProviderConfig
    ) -> None:
        probe_provider(mock_provider, TEST_KEY_VALUE)
        assert (
            mock_server.last_headers().get("Authorization")
            == f"Bearer {TEST_KEY_VALUE}"
        )

    def test_wrong_key_gets_401(
        self, mock_server: MockOpenAIServer, mock_provider: ProviderConfig
    ) -> None:
        mock_server.require_key = "the-right-key"
        result = probe_provider(mock_provider, "wrong-key")
        assert not result.ok
        assert result.status_code == 401
        assert "401" in (result.error or "")

    def test_custom_headers_sent(self, mock_server: MockOpenAIServer) -> None:
        provider = ProviderConfig(
            name="mock",
            base_url=mock_server.base_url,
            headers={"X-Team": "orkestra"},
        )
        result = probe_provider(provider, api_key=None)
        assert result.ok
        assert mock_server.last_headers().get("X-Team") == "orkestra"
        assert "Authorization" not in mock_server.last_headers()

    def test_unreachable(self) -> None:
        provider = ProviderConfig(
            name="down",
            base_url=f"http://127.0.0.1:{unused_port()}",
            timeout_seconds=2,
        )
        result = probe_provider(provider, api_key=None)
        assert not result.ok
        assert result.error is not None
        assert result.status_code is None

    def test_http_error(self, mock_server: MockOpenAIServer) -> None:
        mock_server.mode = "error"
        provider = ProviderConfig(name="mock", base_url=mock_server.base_url)
        result = probe_provider(provider, api_key=None)
        assert not result.ok
        assert result.status_code == 500

    def test_bad_json(self, mock_server: MockOpenAIServer) -> None:
        mock_server.mode = "bad_json"
        provider = ProviderConfig(name="mock", base_url=mock_server.base_url)
        result = probe_provider(provider, api_key=None)
        assert not result.ok
        assert "JSON" in (result.error or "")

    def test_wrong_shape(self, mock_server: MockOpenAIServer) -> None:
        mock_server.mode = "wrong_shape"
        provider = ProviderConfig(name="mock", base_url=mock_server.base_url)
        result = probe_provider(provider, api_key=None)
        assert not result.ok
        assert "data" in (result.error or "")

    def test_timeout(self, mock_server: MockOpenAIServer) -> None:
        mock_server.delay_seconds = 1.0
        provider = ProviderConfig(name="mock", base_url=mock_server.base_url)
        result = probe_provider(provider, api_key=None, timeout=0.2)
        assert not result.ok
        assert "Timeout" in (result.error or "")
