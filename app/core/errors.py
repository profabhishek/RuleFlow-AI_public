# ============================================================
# FILE   : app/core/errors.py
# OWNER  : ROLE 3 — Quality & Audit
# PURPOSE: Centralized FastAPI exception handling. Turns exceptions into
#          clean, uniform JSON — never leaking stack traces to clients.
# ============================================================
"""Centralized exception handling for the FastAPI application.

Every unhandled exception is mapped to a small, uniform JSON body::

    {"success": false, "message": "Invalid rule."}

so clients get a predictable shape and never see internal details or stack
traces. Full diagnostics (including tracebacks for 500s) are logged
server-side via the structured logger in :mod:`app.core.logging`.

Handled exceptions and their HTTP status:

* ``pydantic.ValidationError`` -> 422  (data failed model validation)
* ``ValueError``               -> 400  (bad input; the engine's
  ``RuleValidationError`` / ``OperatorError`` / ``UnknownRuleTypeError`` all
  derive from ``ValueError``, so they land here with their safe messages)
* ``FileNotFoundError``        -> 404  (a required resource was missing)
* ``Exception`` (catch-all)    -> 500  (unexpected; message is generic)

Wiring (done by Role 2 in ``app/main.py``; this module only provides the
callable)::

    from app.core.errors import register_exception_handlers
    register_exception_handlers(app)
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["ErrorResponse", "register_exception_handlers"]


class ErrorResponse(BaseModel):
    """The uniform error body returned to clients."""

    success: bool = False
    message: str


def _error_response(status_code: int, message: str) -> JSONResponse:
    """Build a clean JSON error response with the standard shape."""
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(message=message).model_dump(),
    )


async def handle_validation_error(
    request: Request, exc: ValidationError
) -> JSONResponse:
    """Map model-validation failures to 422 without echoing internals."""
    logger.warning("validation error at %s: %s", request.url.path, exc)
    return _error_response(422, "Validation error.")


async def handle_value_error(request: Request, exc: ValueError) -> JSONResponse:
    """Map bad input (incl. engine ``ValueError`` subclasses) to 400.

    The engine's messages are intentionally human-readable and safe to
    surface (e.g. "Rule 'r': 'outcome' must be a non-empty string.").
    """
    logger.warning("value error at %s: %s", request.url.path, exc)
    return _error_response(400, str(exc) or "Invalid request.")


async def handle_file_not_found(
    request: Request, exc: FileNotFoundError
) -> JSONResponse:
    """Map a missing resource to 404 without revealing filesystem paths."""
    logger.error("file not found at %s: %s", request.url.path, exc)
    return _error_response(404, "Requested resource was not found.")


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all: log the full traceback server-side, return a generic 500."""
    logger.exception("unhandled exception at %s", request.url.path)
    return _error_response(500, "Internal server error.")


def register_exception_handlers(app: FastAPI) -> None:
    """Register all centralized exception handlers on a FastAPI app.

    Starlette dispatches to the most specific handler in the exception's MRO,
    so ``ValidationError`` (a ``ValueError`` subclass) is handled by its own
    handler while other ``ValueError``s fall to :func:`handle_value_error`.
    """
    app.add_exception_handler(ValidationError, handle_validation_error)
    app.add_exception_handler(ValueError, handle_value_error)
    app.add_exception_handler(FileNotFoundError, handle_file_not_found)
    app.add_exception_handler(Exception, handle_unexpected_error)
