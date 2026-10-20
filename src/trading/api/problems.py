"""RFC 7807 Problem Details responses and exception handlers."""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from trading.control.state import VersionConflict
from trading.monitoring.logging import get_logger

PROBLEM_JSON = "application/problem+json"
_log = get_logger(__name__)


class ApiProblem(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        title: str,
        detail: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {title}")
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.extra = dict(extra or {})


def problem_response(request: Request, problem: ApiProblem) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    body: dict[str, Any] = {
        "type": f"urn:trading:problem:{problem.code}",
        "title": problem.title,
        "status": problem.status,
        "detail": problem.detail,
        "instance": request.url.path,
        "code": problem.code,
        "request_id": request_id,
        **problem.extra,
    }
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(body, status_code=problem.status, media_type=PROBLEM_JSON, headers=headers)


def register_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiProblem)
    async def _api_problem(request: Request, exc: ApiProblem) -> JSONResponse:
        return problem_response(request, exc)

    @app.exception_handler(VersionConflict)
    async def _version_conflict(request: Request, exc: VersionConflict) -> JSONResponse:
        problem = ApiProblem(
            409,
            "VERSION_CONFLICT",
            "Stale version",
            detail=str(exc),
            extra={"current_version": exc.current_version},
        )
        return problem_response(request, problem)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo the submitted input back: it may contain secrets.
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        problem = ApiProblem(422, "VALIDATION_ERROR", "Invalid request", extra={"errors": errors})
        return problem_response(request, problem)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        phrase = HTTPStatus(exc.status_code).phrase
        code = phrase.upper().replace(" ", "_").replace("-", "_")
        problem = ApiProblem(exc.status_code, code, phrase, detail=str(exc.detail))
        response = problem_response(request, problem)
        for key, value in (exc.headers or {}).items():
            response.headers[key] = value
        return response

    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    async def _db_unavailable(request: Request, exc: Exception) -> JSONResponse:
        _log.error("database_unavailable", error=type(exc).__name__)
        problem = ApiProblem(503, "DATABASE_UNAVAILABLE", "Database unavailable")
        return problem_response(request, problem)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        _log.exception("unhandled_exception", path=request.url.path)
        return problem_response(request, ApiProblem(500, "INTERNAL", "Internal server error"))
