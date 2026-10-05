"""Explicit exception hierarchy for orkestra.

Every failure mode raises a typed :class:`OrkestraError` subclass carrying a
human-readable message; the CLI renders it in red and exits with
:attr:`OrkestraError.exit_code`. Nothing fails silently.
"""

from __future__ import annotations


class OrkestraError(Exception):
    """Base class for all orkestra errors.

    Attributes:
        exit_code: process exit code the CLI uses for this error.
    """

    exit_code: int = 1


class ConfigError(OrkestraError):
    """The config file is unreadable, malformed, or has an unsupported shape."""


class ValidationError(OrkestraError):
    """User-supplied input (name, URL, option value) failed validation."""


class DuplicateProviderError(OrkestraError):
    """A provider with this name already exists."""


class ProviderNotFoundError(OrkestraError):
    """The referenced provider does not exist."""


class ProviderInUseError(OrkestraError):
    """The provider cannot be removed because models still reference it."""


class DuplicateModelError(OrkestraError):
    """A model with this name already exists."""


class ModelNotFoundError(OrkestraError):
    """The referenced model does not exist."""


class ApiKeyNotConfiguredError(OrkestraError):
    """The provider references an environment variable that is not set."""
