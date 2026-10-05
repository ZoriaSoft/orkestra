"""Shared rich consoles for CLI output."""

from __future__ import annotations

from rich.console import Console

console = Console()
"""stdout console — tables, panels, success messages."""

err_console = Console(stderr=True)
"""stderr console — errors and warnings."""
