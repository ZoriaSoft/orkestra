"""``orkestra providers`` — manage OpenAI-compatible provider endpoints."""

from __future__ import annotations

import os

import typer
from rich.table import Table

from orkestra.client import models_url, probe_provider
from orkestra.errors import ApiKeyNotConfiguredError
from orkestra.schema import ProviderConfig

from .common import (
    command_guard,
    model_validate_or_raise,
    open_registry,
    parse_headers,
    save_registry,
)
from .output import console, err_console

providers_app = typer.Typer(
    name="providers",
    help="Manage OpenAI-compatible provider endpoints.",
    no_args_is_help=True,
)

MAX_LISTED_MODEL_IDS = 20


@providers_app.command("add")
@command_guard
def add(
    name: str = typer.Argument(..., help="Unique provider name (slug)."),
    base_url: str = typer.Option(
        ...,
        "--base-url",
        "-u",
        help="OpenAI-compatible endpoint root, e.g. https://api.openai.com/v1",
    ),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        "-k",
        help="Name of the env var holding the API key (the value is never stored).",
    ),
    header: list[str] | None = typer.Option(
        None,
        "--header",
        "-H",
        help="Extra request header, repeatable: --header 'Name=Value'.",
    ),
    timeout: float = typer.Option(
        15.0, "--timeout", "-t", help="Request timeout in seconds (1-600)."
    ),
) -> None:
    """Register a provider endpoint.

    The API key itself is never written to the config — only the *name* of the
    environment variable it lives in.
    """
    store, registry = open_registry()
    provider: ProviderConfig = model_validate_or_raise(
        ProviderConfig,
        name=name,
        base_url=base_url,
        api_key_env=api_key_env,
        headers=parse_headers(header),
        timeout_seconds=timeout,
    )
    registry.add_provider(provider)
    save_registry(store, registry)

    console.print(
        f"[green]added[/green] provider [bold]{provider.name}[/bold] "
        f"({provider.base_url}) → {store.path}"
    )
    if provider.api_key_env and not os.environ.get(provider.api_key_env):
        err_console.print(
            f"[yellow]warning:[/yellow] env var {provider.api_key_env} is not "
            "set; `orkestra providers test` will fail until you export it"
        )


@providers_app.command("list")
@command_guard
def list_providers() -> None:
    """List registered providers."""
    _, registry = open_registry()
    providers = registry.list_providers()
    if not providers:
        console.print(
            "[dim]No providers registered.[/dim] "
            "Add one: orkestra providers add NAME --base-url URL"
        )
        return

    table = Table(title="Providers", title_justify="left")
    table.add_column("Name", style="bold")
    table.add_column("Base URL")
    table.add_column("API key env")
    table.add_column("Headers", justify="right")
    table.add_column("Timeout", justify="right")
    table.add_column("Models", justify="right")

    for provider in providers:
        model_count = sum(
            1 for m in registry.list_models() if m.provider == provider.name
        )
        table.add_row(
            provider.name,
            provider.base_url,
            provider.api_key_env or "[dim]—[/dim]",
            str(len(provider.headers)) if provider.headers else "[dim]—[/dim]",
            f"{provider.timeout_seconds:g}s",
            str(model_count),
        )
    console.print(table)


@providers_app.command("remove")
@command_guard
def remove(name: str = typer.Argument(..., help="Provider name to remove.")) -> None:
    """Remove a provider. Fails while models still reference it."""
    store, registry = open_registry()
    registry.remove_provider(name)
    save_registry(store, registry)
    console.print(f"[green]removed[/green] provider [bold]{name}[/bold]")


@providers_app.command("test")
@command_guard
def test(
    name: str = typer.Argument(..., help="Provider name to probe."),
    all_models: bool = typer.Option(
        False, "--all", "-a", help="Print every reachable model id."
    ),
) -> None:
    """Probe ``{base_url}/v1/models``: latency + reachable models.

    Exits non-zero when the endpoint is unreachable, answers with an HTTP
    error, or returns an unexpected payload.
    """
    _, registry = open_registry()
    provider = registry.get_provider(name)

    api_key: str | None = None
    if provider.api_key_env:
        api_key = os.environ.get(provider.api_key_env) or None
        if api_key is None:
            raise ApiKeyNotConfiguredError(
                f"provider {name!r} expects its API key in env var "
                f"{provider.api_key_env}, which is not set"
            )

    result = probe_provider(provider, api_key)
    url = models_url(provider.base_url)

    if not result.ok:
        err_console.print(
            f"[bold red]✗ {provider.name}[/bold red] — {url}\n"
            f"  latency: {result.latency_ms:.0f} ms\n"
            f"  error:   {result.error}"
        )
        raise typer.Exit(code=1)

    console.print(
        f"[bold green]✓ {provider.name}[/bold green] — {url}\n"
        f"  latency: {result.latency_ms:.0f} ms\n"
        f"  models:  {len(result.model_ids)} reachable"
    )
    shown = result.model_ids if all_models else result.model_ids[:MAX_LISTED_MODEL_IDS]
    for model_id in shown:
        console.print(f"    • {model_id}")
    hidden = len(result.model_ids) - len(shown)
    if hidden:
        console.print(f"    [dim]… and {hidden} more (--all to show)[/dim]")
