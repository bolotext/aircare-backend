from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from common.responses import error_response


class AppError(Exception):
    """Base application error carrying a stable error code, message, and status code."""

    def __init__(self, code: str, message: str, status_code: int, details: dict | None = None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


class NotFoundError(AppError):
    def __init__(self, message: str = "Resource not found", details: dict | None = None):
        super().__init__("NOT_FOUND", message, status.HTTP_404_NOT_FOUND, details)


class ConflictError(AppError):
    def __init__(self, message: str = "Resource already exists", details: dict | None = None):
        super().__init__("CONFLICT", message, status.HTTP_409_CONFLICT, details)


class UnauthorizedError(AppError):
    def __init__(self, message: str = "Authentication required", details: dict | None = None):
        super().__init__("UNAUTHORIZED", message, status.HTTP_401_UNAUTHORIZED, details)


class ForbiddenError(AppError):
    def __init__(self, message: str = "You do not have permission to perform this action", details: dict | None = None):
        super().__init__("FORBIDDEN", message, status.HTTP_403_FORBIDDEN, details)


class BadRequestError(AppError):
    def __init__(self, message: str = "Invalid request", details: dict | None = None):
        super().__init__("BAD_REQUEST", message, status.HTTP_400_BAD_REQUEST, details)


class UnprocessableEntityError(AppError):
    """A request body that parses fine structurally but fails a business-rule
    check applied at save time (e.g. a prompt referencing an undeclared
    dynamic variable, or a publish-readiness check) — as opposed to
    RequestValidationError, which FastAPI raises for structural/type errors.
    """

    def __init__(self, message: str = "Request could not be processed", details: dict | None = None):
        super().__init__("UNPROCESSABLE_ENTITY", message, status.HTTP_422_UNPROCESSABLE_ENTITY, details)


class TooManyRequestsError(AppError):
    def __init__(self, message: str = "Too many requests", details: dict | None = None):
        super().__init__("TOO_MANY_REQUESTS", message, status.HTTP_429_TOO_MANY_REQUESTS, details)


class BadGatewayError(AppError):
    def __init__(self, message: str = "Upstream service error", details: dict | None = None):
        super().__init__("BAD_GATEWAY", message, status.HTTP_502_BAD_GATEWAY, details)


def register_exception_handlers(app: FastAPI) -> None:
    """Every error response uses the same envelope as success responses
    (see common.responses): {status, code, message, errors}. `code` is the
    HTTP status; the stable machine-readable error type (e.g. "NOT_FOUND")
    and any extra details live inside `errors`.
    """

    # The HTTP transport status is always 200; the real status lives in the
    # envelope's `code` field (see common.responses). Callers must branch on
    # body["code"] / body["status"], not the HTTP status, to detect errors.

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=error_response(
                code=exc.status_code,
                message=exc.message,
                errors={"type": exc.code, **exc.details},
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=error_response(
                code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                message="Request validation failed",
                errors={"type": "VALIDATION_ERROR", "fields": exc.errors()},
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=error_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message="An unexpected error occurred",
                errors={"type": "INTERNAL_SERVER_ERROR"},
            ),
        )
