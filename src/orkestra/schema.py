"""Pydantic schema for orkestra's persisted configuration.

The schema is deliberately small and explicit: providers describe
OpenAI-compatible endpoints, models describe individual models layered for
the orchestra (``tier`` feeds the sef/hamal/kalfa/birlestirici routing in
Phase 2, ``purposes`` describe what a model is good at, ``cost`` feeds the
budget valve).
"""

from __future__ import annotations

import re
from enum import Enum
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
"""Registry keys must be shell-friendly slugs (lowercase, start alnum)."""


class Tier(str, Enum):
    """Cost/quality layer of a model inside the orchestra."""

    STRONG = "strong"
    """Expensive, high-reasoning model (sef, kalfa arbitration, birlestirici)."""

    CHEAP = "cheap"
    """Low-cost model for micro-tasks (hamal pool)."""


class Purpose(str, Enum):
    """What a registered model is meant to be used for."""

    CHAT = "chat"
    CODE = "code"
    MICRO_TASK = "micro-task"


def validate_name(value: str, kind: str) -> str:
    """Validate a registry key.

    Args:
        value: the name supplied by the user.
        kind: ``"provider"`` or ``"model"``, used in the error message.

    Returns:
        The unchanged value when valid.

    Raises:
        ValueError: when the name is not a valid slug.
    """
    if not NAME_PATTERN.match(value):
        raise ValueError(
            f"invalid {kind} name {value!r}: use lowercase letters, digits, "
            "'.', '_' or '-', starting with a letter or digit"
        )
    return value


def validate_base_url(value: str) -> str:
    """Validate and normalize an OpenAI-compatible base URL.

    Accepts ``http://`` and ``https://`` URLs with a host and no query or
    fragment. A ``/v1`` suffix is allowed but not required — the client
    appends ``/v1/models`` when it is missing. The trailing slash is stripped.

    Args:
        value: raw URL string.

    Returns:
        The normalized URL (stripped, no trailing slash).

    Raises:
        ValueError: when the URL is not a plain http(s) host URL.
    """
    stripped = value.strip()
    parsed = urlparse(stripped)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("base_url must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("base_url must include a host")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query string or fragment")
    return stripped.rstrip("/")


class ProviderConfig(BaseModel):
    """An OpenAI-compatible provider endpoint.

    Attributes:
        name: unique registry key (slug).
        base_url: endpoint root, e.g. ``https://api.openai.com/v1``.
        api_key_env: *name* of the environment variable holding the API key.
            The key value itself is never stored — only the variable name.
            ``None`` means the endpoint needs no auth (e.g. local ollama).
        headers: extra HTTP headers sent with every request to the provider.
        timeout_seconds: per-request timeout for probes and engine calls.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    base_url: str
    api_key_env: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=15.0, gt=0, le=600)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_name(value, "provider")

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        return validate_base_url(value)


class CostHint(BaseModel):
    """Optional pricing information, in USD per 1M tokens.

    Used by the Phase 2 budget valve to estimate run cost; values are hints,
    not billing truth.
    """

    model_config = ConfigDict(extra="forbid")

    input_per_1m: float | None = Field(default=None, ge=0)
    output_per_1m: float | None = Field(default=None, ge=0)
    currency: str = "USD"


class ModelConfig(BaseModel):
    """A registered model on a provider.

    Attributes:
        name: unique registry key (slug).
        provider: name of the :class:`ProviderConfig` it belongs to.
        model_id: identifier sent to the provider API. Defaults to ``name`` —
            override it when the remote id is not a valid slug or when you want
            a local alias (e.g. registry ``hamal-a`` -> remote ``gpt-4o-mini``).
        tier: :class:`Tier` routing layer for the orchestra.
        purposes: list of :class:`Purpose` tags.
        cost: optional :class:`CostHint`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    provider: str
    model_id: str | None = None
    tier: Tier
    purposes: list[Purpose] = Field(default_factory=list)
    cost: CostHint | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_name(value, "model")

    @property
    def remote_id(self) -> str:
        """Identifier to send to the provider API (``model_id`` or ``name``)."""
        return self.model_id or self.name


class OrkestraConfig(BaseModel):
    """Root of ``config.yaml``: version + provider and model registries."""

    version: int = 1
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: dict[str, ModelConfig] = Field(default_factory=dict)
