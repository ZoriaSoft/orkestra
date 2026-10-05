"""Unit tests for schema validation: names, URLs, pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from orkestra.schema import (
    CostHint,
    ModelConfig,
    OrkestraConfig,
    ProviderConfig,
    Purpose,
    Tier,
    validate_base_url,
)


class TestBaseUrl:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("https://api.openai.com/v1", "https://api.openai.com/v1"),
            ("https://api.openai.com/v1/", "https://api.openai.com/v1"),
            ("https://api.openai.com", "https://api.openai.com"),
            ("http://localhost:11434/", "http://localhost:11434"),
            ("  https://proxy.local/api  ", "https://proxy.local/api"),
        ],
    )
    def test_valid_urls_normalize(self, raw: str, expected: str) -> None:
        assert validate_base_url(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "ftp://host",            # wrong scheme
            "api.openai.com",        # no scheme
            "https://",              # no host
            "https://host/x?q=1",    # query string
            "https://host/x#frag",   # fragment
            "",                      # empty
        ],
    )
    def test_invalid_urls_rejected(self, raw: str) -> None:
        with pytest.raises(ValueError):
            validate_base_url(raw)


class TestNameValidation:
    @pytest.mark.parametrize("name", ["openai", "nova-proxy", "a1", "x.y_z"])
    def test_valid_names(self, name: str) -> None:
        provider = ProviderConfig(name=name, base_url="https://h.test")
        assert provider.name == name

    @pytest.mark.parametrize(
        "name",
        ["OpenAI", "-lead-dash", "has space", "UPPER", "_leading", ""],
    )
    def test_invalid_names(self, name: str) -> None:
        with pytest.raises(PydanticValidationError):
            ProviderConfig(name=name, base_url="https://h.test")


class TestProviderConfig:
    def test_defaults(self) -> None:
        provider = ProviderConfig(name="p", base_url="https://h.test")
        assert provider.api_key_env is None
        assert provider.headers == {}
        assert provider.timeout_seconds == 15.0

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ProviderConfig(
                name="p", base_url="https://h.test", api_key_value="oops"  # type: ignore[call-arg]
            )

    def test_timeout_bounds(self) -> None:
        with pytest.raises(PydanticValidationError):
            ProviderConfig(name="p", base_url="https://h.test", timeout_seconds=0)
        with pytest.raises(PydanticValidationError):
            ProviderConfig(name="p", base_url="https://h.test", timeout_seconds=601)


class TestModelConfig:
    def test_remote_id_defaults_to_name(self) -> None:
        model = ModelConfig(name="m", provider="p", tier=Tier.CHEAP)
        assert model.remote_id == "m"

    def test_remote_id_override(self) -> None:
        model = ModelConfig(
            name="alias", provider="p", tier=Tier.STRONG, model_id="GPT-5 Real/Name"
        )
        assert model.remote_id == "GPT-5 Real/Name"

    def test_purposes_and_cost(self) -> None:
        model = ModelConfig(
            name="m",
            provider="p",
            tier=Tier.CHEAP,
            purposes=[Purpose.CHAT, Purpose.MICRO_TASK],
            cost=CostHint(input_per_1m=0.1, output_per_1m=0.2),
        )
        assert Purpose.CODE not in model.purposes
        assert model.cost is not None and model.cost.currency == "USD"

    def test_negative_cost_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            CostHint(input_per_1m=-1.0)


class TestOrkestraConfig:
    def test_empty_default(self) -> None:
        config = OrkestraConfig()
        assert config.version == 1
        assert config.providers == {}
        assert config.models == {}

    def test_yaml_roundtrip_shape(self) -> None:
        config = OrkestraConfig(
            providers={"p": ProviderConfig(name="p", base_url="https://h.test")},
            models={"m": ModelConfig(name="m", provider="p", tier=Tier.CHEAP)},
        )
        dumped = config.model_dump(mode="json")
        restored = OrkestraConfig.model_validate(dumped)
        assert restored == config
        assert restored.models["m"].tier is Tier.CHEAP
