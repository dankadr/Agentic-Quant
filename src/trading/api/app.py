"""FastAPI application factory."""

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

import trading
from trading.api.middleware import MetricsMiddleware, RequestIdMiddleware
from trading.api.problems import register_handlers
from trading.api.routes import (
    agent_runs,
    agents,
    audit,
    control,
    dashboard,
    health,
    portfolio,
    quant,
    research_docs,
    system,
    transactions,
    ui,
)
from trading.core.clock import Clock
from trading.core.config import AgentsConfig, AppConfig, ConfigError, Settings, load_config
from trading.core.release import release_info
from trading.data.calendar import MarketCalendar
from trading.db.migrate import schema_head
from trading.documents.blobs import BlobStore


def _yaml_agents(settings: Settings, config: AppConfig) -> AgentsConfig:
    try:
        return load_config(settings.config_dir).agents
    except ConfigError:
        return config.agents


def create_app(
    *,
    settings: Settings,
    config: AppConfig,
    session_factory: sessionmaker[Session],
    clock: Clock,
    calendar: MarketCalendar | None = None,
) -> FastAPI:
    app = FastAPI(title="Trading API", version=trading.__version__)
    app.state.settings = settings
    app.state.config = config
    # The YAML defaults: what agent settings are before any dashboard edit. The agents routes
    # overlay applied edits per request, so they stay right after a service restarts for a reload.
    app.state.yaml_agents = _yaml_agents(settings, config)
    app.state.session_factory = session_factory
    app.state.clock = clock
    app.state.calendar = calendar
    app.state.blobs = BlobStore(settings.document_store_dir)
    app.state.release = release_info(
        revision=settings.release_revision,
        image=settings.release_image,
        config_dir=settings.config_dir,
        schema_head=schema_head(),
    )
    register_handlers(app)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestIdMiddleware)  # added last = outermost
    app.include_router(health.router)
    app.include_router(system.router)
    app.include_router(control.router)
    app.include_router(audit.router)
    app.include_router(portfolio.router)
    app.include_router(quant.router)
    app.include_router(dashboard.router)
    app.include_router(agents.router)  # after dashboard: /v1/agents/runs must match first
    app.include_router(agent_runs.router)
    app.include_router(transactions.router)
    app.include_router(research_docs.router)
    app.include_router(ui.router)
    return app
