"""``orkestra config`` — inspect where/how configuration is stored."""

from __future__ import annotations

import typer
import yaml

from orkestra.config import HOME_ENV_VAR, ConfigStore

from .common import command_guard, open_registry
from .output import console

config_app = typer.Typer(
    name="config",
    help="Inspect orkestra configuration storage.",
    no_args_is_help=True,
)


@config_app.command("path")
@command_guard
def path() -> None:
    """Print the active config file path (honours ORKESTRA_HOME)."""
    store = ConfigStore()
    state = "exists" if store.exists() else "will be created on first write"
    console.print(
        f"{store.path}  [dim]({state}; override home with {HOME_ENV_VAR})[/dim]"
    )


@config_app.command("show")
@command_guard
def show() -> None:
    """Print the current config file contents (env var names, never secrets)."""
    _, registry = open_registry()
    console.print(
        yaml.safe_dump(
            registry.config.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
        ).rstrip()
    )
