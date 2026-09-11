"""Configuration CLI commands."""

from __future__ import annotations

import click
from rich.table import Table

from myloware.cli.ui import console
from myloware.config import settings


@click.group()
def config() -> None:
    """Inspect configuration."""


@config.command("show")
def config_show() -> None:
    """Show key configuration settings."""
    table = Table(title="AISMR Configuration", show_lines=False)
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("Llama Stack URL", settings.llama_stack_url)
    table.add_row("Model", settings.llama_stack_model)
    table.add_row("Project ID", settings.project_id)
    table.add_row("Vector DB Provider", "milvus" if settings.milvus_uri else "pgvector")
    table.add_row(
        "Database URL",
        (
            settings.database_url.split("@")[-1]
            if "@" in settings.database_url
            else settings.database_url
        ),
    )
    table.add_row("API Host", settings.api_host)
    table.add_row("API Port", str(settings.api_port))
    table.add_row("Environment", settings.environment)

    console.print(table)


def register(cli: click.Group) -> None:
    cli.add_command(config)
