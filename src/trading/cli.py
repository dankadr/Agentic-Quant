"""Command line entrypoint for all services and admin tools."""

import typer

import trading

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Paper-only trading system."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(trading.__version__)
