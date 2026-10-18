"""Normalized error types and FastAPI exception handlers.

Every API error is returned in one shape:

    {"error": {"code": "...", "message": "...", "details": {...} | null}}
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class TitanError(Exception):
    """Base application error carrying a stable machine-readable code."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details


class NotFoundError(TitanError):
    status_code = 404
    code = "not_found"


class ValidationError(TitanError):
    status_code = 422
    code = "validation_error"


class STTUnavailableError(TitanError):
    status_code = 503
    code = "stt_unavailable"


class ProviderUnavailableError(TitanError):
    status_code = 503
    code = "provider_unavailable"


class ExtractionUnavailableError(TitanError):
    status_code = 503
    code = "extraction_unavailable"


class ConflictError(TitanError):
    status_code = 409
    code = "conflict"


class AuthenticationError(TitanError):
    """No usable credential was presented (or it expired / failed signature)."""

    status_code = 401
    code = "unauthenticated"


class AuthorizationError(TitanError):
    """A valid principal asked for a resource it does not own."""

    status_code = 403
    code = "forbidden"


class AuthUnavailableError(TitanError):
    """Authentication cannot be served safely with the current configuration."""

    status_code = 503
    code = "auth_unavailable"


class NormalizationError(TitanError):
    status_code = 422
    code = "normalization_error"


class SealUnavailableError(TitanError):
    status_code = 503
    code = "seal_unavailable"


class IncompleteFactSheetError(TitanError):
    status_code = 422
    code = "incomplete_factsheet"


class TokenError(TitanError):
    status_code = 422
    code = "token_error"


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def _jsonable(value: Any) -> Any:
    """Recursively coerce validation details into JSON-serializable values.

    Pydantic validation errors embed the original exception objects in `ctx`
    (e.g. a bare `ValueError`), which the stdlib JSON encoder rejects.
    """
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, BaseException):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(TitanError)
    async def _titan_error(_: Request, exc: TitanError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body(
                "validation_error", "Request validation failed.", _jsonable(exc.errors())
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body("http_error", str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Never leak internals to clients; details stay in server logs.
        return JSONResponse(
            status_code=500,
            content=error_body("internal_error", "An unexpected error occurred."),
        )
