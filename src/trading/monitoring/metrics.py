"""Prometheus metrics. Labels are low-cardinality: no symbols, accounts or strategies."""

from prometheus_client import Counter, Gauge, start_http_server

HTTP_REQUESTS = Counter(
    "trading_http_requests_total", "HTTP requests by route template", ["method", "route", "status"]
)
JOBS_PROCESSED = Counter(
    "trading_jobs_processed_total", "Jobs processed by kind and outcome", ["kind", "outcome"]
)
LEASE_HELD = Gauge("trading_lease_held", "1 while this process holds the named lease", ["name"])
ORDERS_BY_STATUS = Counter("trading_orders_total", "Order status transitions", ["status"])


def start_metrics_server(port: int | None) -> None:
    """Serve /metrics on an internal port. A no-op when ``port`` is None."""
    if port is not None:
        start_http_server(port)
