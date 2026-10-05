"""Helpers shared by CLI command groups.

- :func:`command_guard` turns :class:`OrkestraError` into a red message +
  non-zero ``typer.Exit``.
- :func:`open_registry` loads the config into a :class:`Registry`.
- :func:`save_registry` persists mutations.
- :func:`parse_headers` validates repeated ``--header K=V`` options.
- :func:`model_validate_or_raise` converts pydantic errors into our
  :class:`ValidationError` with a compact message.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

import typer
from pydantic import BaseModel, ValidationError as PydanticValidationError

from orkestra.config import ConfigStore
from orkestra.errors import OrkestraError, ValidationError
from orkestra.registry import Registry

from .output import err_console

F = TypeVar("F", bound=Callable[..., Any])


def command_guard(fn: F) -> F:
    """Decorator: render OrkestraError as ``error: ...`` + exit code."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except OrkestraError as exc:
            err_console.print(f"[bold red]error:[/bold red] {exc}")
            raise typer.Exit(code=exc.exit_code) from exc

    return wrapper  # type: ignore[return-value]


def open_registry() -> tuple[ConfigStore, Registry]:
    """Load the config from the active home dir into a fresh Registry."""
    store = ConfigStore()
    return store, Registry(store.load())


def save_registry(store: ConfigStore, registry: Registry) -> None:
    """Persist a mutated registry back to disk."""
    store.save(registry.config)


def parse_headers(raw: list[str] | None) -> dict[str, str]:
    """Parse repeated ``--header 'Name=Value'`` options into a dict.

    Raises:
        typer.BadParameter: an entry lacks ``=`` or has an empty name.
    """
    headers: dict[str, str] = {}
    for item in raw or []:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise typer.BadParameter(
                f"invalid header {item!r}; use the form 'Name=Value'"
            )
        headers[name.strip()] = value.strip()
    return headers


def model_validate_or_raise(model_cls: type[BaseModel], **kwargs: Any) -> Any:
    """Construct a pydantic model, converting failures to ValidationError.

    Pydantic's default error dump is noisy for CLI users; this keeps the
    ``loc -> message`` pairs and drops the boilerplate.
    """
    try:
        return model_cls(**kwargs)
    except PydanticValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        raise ValidationError(details) from exc
