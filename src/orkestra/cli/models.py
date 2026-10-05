"""``orkestra models`` — manage the model registry."""

from __future__ import annotations

import typer
from rich.table import Table

from orkestra.schema import CostHint, ModelConfig, Purpose, Tier

from .common import (
    command_guard,
    model_validate_or_raise,
    open_registry,
    save_registry,
)
from .output import console

models_app = typer.Typer(
    name="models",
    help="Manage the model registry (tier/purpose/cost metadata).",
    no_args_is_help=True,
)


@models_app.command("add")
@command_guard
def add(
    name: str = typer.Argument(..., help="Unique model name (slug)."),
    provider: str = typer.Option(
        ..., "--provider", "-p", help="Provider this model is served by."
    ),
    tier: Tier = typer.Option(
        ..., "--tier", "-t", help="strong (sef/kalfa/birlestirici) or cheap (hamal)."
    ),
    purpose: list[Purpose] | None = typer.Option(
        None,
        "--purpose",
        help="What the model is for; repeatable: --purpose chat --purpose code.",
    ),
    model_id: str | None = typer.Option(
        None,
        "--model-id",
        help="Remote id sent to the API when it differs from the registry name.",
    ),
    cost_in: float | None = typer.Option(
        None, "--cost-in", help="USD per 1M input tokens (hint)."
    ),
    cost_out: float | None = typer.Option(
        None, "--cost-out", help="USD per 1M output tokens (hint)."
    ),
) -> None:
    """Register a model on an existing provider."""
    store, registry = open_registry()
    cost = (
        CostHint(input_per_1m=cost_in, output_per_1m=cost_out)
        if cost_in is not None or cost_out is not None
        else None
    )
    model: ModelConfig = model_validate_or_raise(
        ModelConfig,
        name=name,
        provider=provider,
        model_id=model_id,
        tier=tier,
        purposes=purpose or [],
        cost=cost,
    )
    registry.add_model(model)
    save_registry(store, registry)
    console.print(
        f"[green]added[/green] model [bold]{model.name}[/bold] "
        f"({model.provider}, tier={model.tier.value})"
    )


@models_app.command("list")
@command_guard
def list_models(
    tier: Tier | None = typer.Option(
        None, "--tier", "-t", help="Filter by tier."
    ),
    purpose: Purpose | None = typer.Option(
        None, "--purpose", help="Filter by purpose."
    ),
    provider: str | None = typer.Option(
        None, "--provider", "-p", help="Filter by provider name."
    ),
) -> None:
    """List registered models, optionally filtered."""
    _, registry = open_registry()
    models = registry.list_models(tier=tier, purpose=purpose, provider=provider)
    if not models:
        console.print(
            "[dim]No models registered.[/dim] "
            "Add one: orkestra models add NAME --provider P --tier cheap"
        )
        return

    table = Table(title="Models", title_justify="left")
    table.add_column("Name", style="bold")
    table.add_column("Provider")
    table.add_column("Remote id")
    table.add_column("Tier")
    table.add_column("Purposes")
    table.add_column("Cost/1M (in/out)")

    for model in models:
        tier_style = "magenta" if model.tier is Tier.STRONG else "yellow"
        cost = (
            f"{model.cost.input_per_1m} / {model.cost.output_per_1m} {model.cost.currency}"
            if model.cost
            else "[dim]—[/dim]"
        )
        table.add_row(
            model.name,
            model.provider,
            model.remote_id,
            f"[{tier_style}]{model.tier.value}[/{tier_style}]",
            ", ".join(p.value for p in model.purposes) or "[dim]—[/dim]",
            cost,
        )
    console.print(table)


@models_app.command("remove")
@command_guard
def remove(name: str = typer.Argument(..., help="Model name to remove.")) -> None:
    """Remove a model from the registry."""
    store, registry = open_registry()
    registry.remove_model(name)
    save_registry(store, registry)
    console.print(f"[green]removed[/green] model [bold]{name}[/bold]")
