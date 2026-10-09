"""``orkestra run`` — execute a task through the full orchestra."""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from orkestra.chat import HttpChatClient
from orkestra.engine import Orchestra
from orkestra.errors import ValidationError

from .common import command_guard, open_registry
from .output import console

_STATUS_STYLE = {
    "ok": "bold green",
    "partial": "bold yellow",
    "failed": "bold red",
    "budget_exceeded": "bold red",
}


@command_guard
def run_command(
    task: str = typer.Argument(..., help="The task to orchestrate."),
    budget: float | None = typer.Option(
        None, "--budget", "-b", help="USD budget valve; needs cost hints on every model."
    ),
    token_budget: int | None = typer.Option(
        None, "--token-budget", help="Total token budget across the run."
    ),
    strong: str | None = typer.Option(
        None,
        "--strong",
        "-s",
        help="Registry name of the strong model (sef/kalfa/birlestirici).",
    ),
    max_parallel: int = typer.Option(
        4, "--max-parallel", help="Max hamal workers running in parallel."
    ),
    arbitrate: bool = typer.Option(
        True,
        "--arbitrate/--no-arbitrate",
        help="Let the strong model referee acceptance criteria (kalfa stage 2).",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the raw run report as JSON."
    ),
) -> None:
    """Run one task: sef decomposes, hamal pool executes, kalfa validates,
    birlestirici synthesizes."""
    if budget is not None and budget <= 0:
        raise ValidationError("--budget must be greater than 0 (USD)")
    if token_budget is not None and token_budget <= 0:
        raise ValidationError("--token-budget must be a positive integer")
    if max_parallel < 1:
        raise ValidationError("--max-parallel must be at least 1")
    _, registry = open_registry()
    orchestra = Orchestra(
        registry,
        HttpChatClient(),
        strong_model=strong,
        max_parallel=max_parallel,
        arbitrate=arbitrate,
        budget_usd=budget,
        token_budget=token_budget,
    )
    report = orchestra.run(task)

    if as_json:
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _render(report)

    raise typer.Exit(code=0 if report["status"] == "ok" else 1)


def _render(report: dict[str, Any]) -> None:
    status = report["status"]
    console.print(
        f"run [bold]{report['run_id']}[/bold] — "
        f"[{_STATUS_STYLE.get(status, 'white')}]{status}[/{_STATUS_STYLE.get(status, 'white')}]"
    )

    table = Table(title="Pieces", title_justify="left")
    table.add_column("id", style="bold")
    table.add_column("status")
    table.add_column("attempts", justify="right")
    table.add_column("models")
    table.add_column("error", overflow="fold")
    for piece in report["pieces"]:
        status_style = "green" if piece["status"] == "passed" else (
            "cyan" if piece["status"] == "escalated"
            else "yellow" if piece["status"] == "cancelled" else "red"
        )
        table.add_row(
            piece["id"],
            f"[{status_style}]{piece['status']}[/{status_style}]",
            str(len(piece["attempts"])),
            ", ".join(a["model"] for a in piece["attempts"]),
            escape(piece["error"]) if piece["error"] else "[dim]—[/dim]",
        )
    console.print(table)

    if report["result"] is not None:
        result = report["result"]
        body = (
            json.dumps(result, ensure_ascii=False, indent=2)
            if isinstance(result, (dict, list))
            else str(result)
        )
        console.print(Panel(escape(body), title="Result", title_align="left"))

    totals = report["usage"]["totals"]
    console.print(
        f"[dim]usage:[/dim] {len(report['usage']['calls'])} calls · "
        f"{totals['prompt_tokens']} prompt + {totals['completion_tokens']} "
        f"completion tokens · est. ${totals['cost_usd']}"
    )
    for error in report["errors"]:
        console.print(f"[red]error:[/red] {escape(error)}")
