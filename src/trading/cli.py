"""Command line entrypoint for all services and admin tools."""

import typer
from sqlalchemy.exc import IntegrityError

import trading
from trading.api.auth import Role, create_principal
from trading.core.clock import SystemClock
from trading.core.config import Settings
from trading.db.engine import make_engine, make_session_factory

app = typer.Typer(no_args_is_help=True, add_completion=False)
admin_app = typer.Typer(no_args_is_help=True, help="Administrative commands.")
app.add_typer(admin_app, name="admin")


@app.callback()
def main() -> None:
    """Paper-only trading system."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(trading.__version__)


@admin_app.command("create-api-key")
def create_api_key(
    name: str = typer.Option(..., help="Unique principal name."),
    role: Role = typer.Option(..., help="viewer (read-only) or operator."),
) -> None:
    """Create a principal and print its API key once, on stdout. The key is never stored."""
    engine = make_engine(Settings().resolved_database_url())
    try:
        with make_session_factory(engine).begin() as session:
            _, key = create_principal(session, name=name, role=role, now=SystemClock().now())
    except IntegrityError:
        typer.echo(f"principal {name!r} already exists", err=True)
        raise typer.Exit(1) from None
    finally:
        engine.dispose()
    typer.echo(key)
