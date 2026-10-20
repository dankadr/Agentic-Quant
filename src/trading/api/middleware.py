"""Request correlation IDs and HTTP metrics."""

import re
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from trading.core.ids import new_id
from trading.monitoring.logging import bind_request_id, clear_context
from trading.monitoring.metrics import HTTP_REQUESTS

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

CallNext = Callable[[Request], Awaitable[Response]]


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Accept a well-formed ``X-Request-ID`` or generate one; echo it and bind it to the logs."""

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else str(new_id())
        request.state.request_id = request_id
        bind_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            clear_context()
        response.headers["X-Request-ID"] = request_id
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """Count requests by method, route *template* and status (never raw paths)."""

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            HTTP_REQUESTS.labels(request.method, template, str(status)).inc()
