"""In-memory registry: CRUD over :class:`OrkestraConfig` plus resolution.

The :class:`Registry` is the seam between Phase 1 (config management) and the
Phase 2 engine: CLI commands mutate it then persist via
:class:`orkestra.config.ConfigStore`; the engine calls
:meth:`Registry.resolve` to turn a model name into a fully-resolved endpoint
(provider URL + model id + API key) without touching YAML itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from orkestra.errors import (
    ApiKeyNotConfiguredError,
    DuplicateModelError,
    DuplicateProviderError,
    ModelNotFoundError,
    ProviderInUseError,
    ProviderNotFoundError,
)
from orkestra.schema import (
    ModelConfig,
    OrkestraConfig,
    ProviderConfig,
    Purpose,
    Tier,
)


@dataclass(frozen=True)
class ResolvedModel:
    """A model joined with its provider and resolved API key.

    ``api_key`` holds the *secret value* read from the environment at resolve
    time — it is never written to disk and must never be logged.
    """

    model: ModelConfig
    provider: ProviderConfig
    model_id: str
    api_key: str | None


class Registry:
    """CRUD operations over an :class:`OrkestraConfig`.

    Mutations happen in memory only; the caller decides when to persist via
    ``ConfigStore.save(registry.config)``.
    """

    def __init__(self, config: OrkestraConfig | None = None) -> None:
        self._config = config or OrkestraConfig()

    @property
    def config(self) -> OrkestraConfig:
        """The underlying config object (mutated in place)."""
        return self._config

    # ------------------------------------------------------------------
    # providers
    # ------------------------------------------------------------------

    def add_provider(self, provider: ProviderConfig) -> None:
        """Register a provider.

        Raises:
            DuplicateProviderError: a provider with this name exists.
        """
        if provider.name in self._config.providers:
            raise DuplicateProviderError(f"provider {provider.name!r} already exists")
        self._config.providers[provider.name] = provider

    def get_provider(self, name: str) -> ProviderConfig:
        """Return a provider by name.

        Raises:
            ProviderNotFoundError: no provider with this name.
        """
        try:
            return self._config.providers[name]
        except KeyError:
            raise ProviderNotFoundError(
                f"provider {name!r} not found; run `orkestra providers list`"
            ) from None

    def remove_provider(self, name: str) -> None:
        """Remove a provider.

        Raises:
            ProviderNotFoundError: no provider with this name.
            ProviderInUseError: registered models still reference it.
        """
        self.get_provider(name)
        users = sorted(m.name for m in self._config.models.values() if m.provider == name)
        if users:
            raise ProviderInUseError(
                f"provider {name!r} is used by models: {', '.join(users)}; remove them first"
            )
        del self._config.providers[name]

    def list_providers(self) -> list[ProviderConfig]:
        """All providers, sorted by name."""
        return sorted(self._config.providers.values(), key=lambda p: p.name)

    # ------------------------------------------------------------------
    # models
    # ------------------------------------------------------------------

    def add_model(self, model: ModelConfig) -> None:
        """Register a model. Its provider must already exist.

        Raises:
            DuplicateModelError: a model with this name exists.
            ProviderNotFoundError: the referenced provider does not exist.
        """
        if model.name in self._config.models:
            raise DuplicateModelError(f"model {model.name!r} already exists")
        self.get_provider(model.provider)
        self._config.models[model.name] = model

    def get_model(self, name: str) -> ModelConfig:
        """Return a model by name.

        Raises:
            ModelNotFoundError: no model with this name.
        """
        try:
            return self._config.models[name]
        except KeyError:
            raise ModelNotFoundError(
                f"model {name!r} not found; run `orkestra models list`"
            ) from None

    def remove_model(self, name: str) -> None:
        """Remove a model.

        Raises:
            ModelNotFoundError: no model with this name.
        """
        self.get_model(name)
        del self._config.models[name]

    def list_models(
        self,
        tier: Tier | None = None,
        purpose: Purpose | None = None,
        provider: str | None = None,
    ) -> list[ModelConfig]:
        """Models matching the given filters, sorted by name.

        Args:
            tier: keep only this tier when given.
            purpose: keep only models advertising this purpose when given.
            provider: keep only models on this provider when given. The
                provider name is not validated — an unknown name simply
                yields an empty list.
        """
        models = self._config.models.values()
        if tier is not None:
            models = [m for m in models if m.tier == tier]
        if purpose is not None:
            models = [m for m in models if purpose in m.purposes]
        if provider is not None:
            models = [m for m in models if m.provider == provider]
        return sorted(models, key=lambda m: m.name)

    # ------------------------------------------------------------------
    # resolution (Phase 2 engine seam)
    # ------------------------------------------------------------------

    def resolve(self, name: str) -> ResolvedModel:
        """Resolve a model name to endpoint + remote id + API key.

        Reads the API key from the environment variable named by
        ``provider.api_key_env``. The returned key is a live secret: never
        log or persist it.

        Raises:
            ModelNotFoundError: no model with this name.
            ProviderNotFoundError: the model's provider is missing.
            ApiKeyNotConfiguredError: the provider expects an env var that is
                not set or empty.
        """
        model = self.get_model(name)
        provider = self.get_provider(model.provider)
        api_key: str | None = None
        if provider.api_key_env:
            api_key = os.environ.get(provider.api_key_env) or None
            if api_key is None:
                raise ApiKeyNotConfiguredError(
                    f"provider {provider.name!r} expects its API key in env var "
                    f"{provider.api_key_env}, which is not set"
                )
        return ResolvedModel(
            model=model,
            provider=provider,
            model_id=model.remote_id,
            api_key=api_key,
        )
