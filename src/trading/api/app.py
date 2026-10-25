"""FastAPI application factory."""

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

import trading
from trading.api.middleware import MetricsMiddleware, RequestIdMiddleware
from trading.api.problems import register_handlers
from trading.api.routes import (
    audit,
    control,
    dashboard,
    health,
    portfolio,
    quant,
    system,
    transactions,
)
from trading.core.clock import Clock
from trading.core.config import AppConfig, Settings
from trading.data.calendar import MarketCalendar


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
    app.state.session_factory = session_factory
    app.state.clock = clock
    app.state.calendar = calendar
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
    app.include_router(transactions.router)
    return app
