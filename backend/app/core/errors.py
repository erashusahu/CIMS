import logging
from http import HTTPStatus

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

logger = logging.getLogger("cims.errors")


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message


def count_freeze_error(exc: Exception) -> bool:
    """Recognize only the database guard's known count-freeze failure."""
    original = getattr(exc, "orig", None)
    return getattr(
        original, "sqlstate", None
    ) == "23514" and "stock is frozen for an active count" in str(original)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(request, exc.status_code, exc.code, exc.message)


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    fields: list[dict] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    error = {"code": code, "message": message, "request_id": request_id}
    if fields is not None:
        error["fields"] = fields
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers={**(headers or {}), "X-Request-ID": request_id},
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Pydantic's input and ctx can contain credentials and non-JSON exceptions.
    fields = [
        {"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()
    ]
    return error_response(
        request, 422, "validation_error", "Please check the submitted fields", fields=fields
    )


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return error_response(
        request,
        exc.status_code,
        {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error"),
        HTTPStatus(exc.status_code).phrase,
        headers=exc.headers,
    )


def log_failure(request: Request, exc: Exception) -> None:
    # SQLAlchemy exception text includes statements and bound values. Never log it.
    logger.error(
        "request_failed",
        extra={
            "request_id": getattr(request.state, "request_id", "unknown"),
            "error_type": type(exc).__name__,
        },
    )


async def database_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log_failure(request, exc)
    return error_response(request, 500, "database_error", "The request could not be completed")


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log_failure(request, exc)
    return error_response(request, 500, "internal_error", "The request could not be completed")
