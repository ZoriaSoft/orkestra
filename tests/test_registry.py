"""Unit tests for Registry: provider/model CRUD, filters, resolution."""

from __future__ import annotations

import pytest

from orkestra.errors import (
    ApiKeyNotConfiguredError,
    DuplicateModelError,
    DuplicateProviderError,
    ModelNotFoundError,
    ProviderInUseError,
    ProviderNotFoundError,
)
from orkestra.registry import Registry
from orkestra.schema import ModelConfig, ProviderConfig, Purpose, Tier

from .conftest import TEST_KEY_ENV, TEST_KEY_VALUE


def _provider(name: str = "p1") -> ProviderConfig:
    return ProviderConfig(name=name, base_url="https://h.test", api_key_env=TEST_KEY_ENV)


def _model(name: str = "m1", provider: str = "p1", tier: Tier = Tier.CHEAP) -> ModelConfig:
    return ModelConfig(name=name, provider=provider, tier=tier)


class TestProviders:
    def test_add_get_list(self) -> None:
        registry = Registry()
        registry.add_provider(_provider("b"))
        registry.add_provider(_provider("a"))
        assert [p.name for p in registry.list_providers()] == ["a", "b"]
        assert registry.get_provider("b").base_url == "https://h.test"

    def test_add_duplicate(self) -> None:
        registry = Registry()
        registry.add_provider(_provider())
        with pytest.raises(DuplicateProviderError):
            registry.add_provider(_provider())

    def test_get_missing(self) -> None:
        with pytest.raises(ProviderNotFoundError, match="ghost"):
            Registry().get_provider("ghost")

    def test_remove(self) -> None:
        registry = Registry()
        registry.add_provider(_provider())
        registry.remove_provider("p1")
        assert registry.list_providers() == []

    def test_remove_missing(self) -> None:
        with pytest.raises(ProviderNotFoundError):
            Registry().remove_provider("ghost")

    def test_remove_in_use_blocked(self) -> None:
        registry = Registry()
        registry.add_provider(_provider())
        registry.add_model(_model())
        with pytest.raises(ProviderInUseError, match="m1"):
            registry.remove_provider("p1")
        registry.remove_model("m1")
        registry.remove_provider("p1")  # now allowed


class TestModels:
    def _seeded(self) -> Registry:
        registry = Registry()
        registry.add_provider(_provider("cheap-pool"))
        registry.add_provider(_provider("premium"))
        registry.add_model(
            ModelConfig(
                name="hamal-1",
                provider="cheap-pool",
                tier=Tier.CHEAP,
                purposes=[Purpose.MICRO_TASK],
            )
        )
        registry.add_model(
            ModelConfig(
                name="sef-1",
                provider="premium",
                tier=Tier.STRONG,
                purposes=[Purpose.CHAT, Purpose.CODE],
            )
        )
        return registry

    def test_add_requires_provider(self) -> None:
        registry = Registry()
        with pytest.raises(ProviderNotFoundError):
            registry.add_model(_model(provider="missing"))

    def test_add_duplicate(self) -> None:
        registry = self._seeded()
        with pytest.raises(DuplicateModelError):
            registry.add_model(_model(name="hamal-1", provider="cheap-pool"))

    def test_get_remove(self) -> None:
        registry = self._seeded()
        assert registry.get_model("hamal-1").tier is Tier.CHEAP
        registry.remove_model("hamal-1")
        with pytest.raises(ModelNotFoundError):
            registry.get_model("hamal-1")

    def test_remove_missing(self) -> None:
        with pytest.raises(ModelNotFoundError):
            self._seeded().remove_model("ghost")

    def test_list_filter_by_tier(self) -> None:
        registry = self._seeded()
        cheap = registry.list_models(tier=Tier.CHEAP)
        assert [m.name for m in cheap] == ["hamal-1"]
        assert [m.name for m in registry.list_models(tier=Tier.STRONG)] == ["sef-1"]

    def test_list_filter_by_purpose(self) -> None:
        registry = self._seeded()
        assert [m.name for m in registry.list_models(purpose=Purpose.CODE)] == ["sef-1"]
        assert registry.list_models(purpose=Purpose.MICRO_TASK)[0].name == "hamal-1"

    def test_list_filter_by_provider(self) -> None:
        registry = self._seeded()
        assert [m.name for m in registry.list_models(provider="premium")] == ["sef-1"]
        assert registry.list_models(provider="unknown") == []

    def test_list_sorted(self) -> None:
        registry = self._seeded()
        assert [m.name for m in registry.list_models()] == ["hamal-1", "sef-1"]


class TestResolve:
    def test_resolve_with_key(
        self, mock_provider: ProviderConfig, api_key_env: str
    ) -> None:
        registry = Registry()
        registry.add_provider(mock_provider)
        registry.add_model(_model(provider="mock"))
        resolved = registry.resolve("m1")
        assert resolved.api_key == TEST_KEY_VALUE
        assert resolved.provider.name == "mock"
        assert resolved.model_id == "m1"

    def test_resolve_missing_env(
        self, mock_provider: ProviderConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(TEST_KEY_ENV, raising=False)
        registry = Registry()
        registry.add_provider(mock_provider)
        registry.add_model(_model(provider="mock"))
        with pytest.raises(ApiKeyNotConfiguredError, match=TEST_KEY_ENV):
            registry.resolve("m1")

    def test_resolve_no_auth_provider(self) -> None:
        registry = Registry()
        registry.add_provider(
            ProviderConfig(name="local", base_url="http://localhost:11434")
        )
        registry.add_model(_model(provider="local"))
        assert registry.resolve("m1").api_key is None

    def test_resolve_uses_remote_id(self, api_key_env: str) -> None:
        registry = Registry()
        registry.add_provider(_provider())
        registry.add_model(
            ModelConfig(
                name="alias", provider="p1", tier=Tier.STRONG, model_id="real-id/9"
            )
        )
        assert registry.resolve("alias").model_id == "real-id/9"
