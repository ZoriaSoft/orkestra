"""Top-level typer application and console entry point."""

from __future__ import annotations

import typer

from orkestra import __version__

from .config_cmds import config_app
from .models import models_app
from .providers import providers_app
from .run import run_command

app = typer.Typer(
    name="orkestra",
    help=(
        "Open-source LLM provider/model registry and layered orchestra "
        "engine: `orkestra run` decomposes a task (sef, strong model), "
        "executes pieces on cheap models (hamal pool), validates every "
        "output (kalfa) and synthesizes the result (birlestirici)."
    ),
    no_args_is_help=True,
    add_completion=False,
)

app.add_typer(providers_app, name="providers")
app.add_typer(models_app, name="models")
app.add_typer(config_app, name="config")
app.command("run")(run_command)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"orkestra {__version__}")
        raise typer.Exit()


@app.callback()
def _callback(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """orkestra — provider/model registry for layered LLM orchestration."""


def main() -> None:
    """Console script entry point (``orkestra``)."""
    app()


if __name__ == "__main__":
    main()
