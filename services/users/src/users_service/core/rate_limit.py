from common.responses import error_response
from fastapi import Request, status
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Matches the platform-wide {status, code, message, errors} envelope,
    unlike slowapi's built-in handler which returns its own shape.
    """
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=error_response(
            code=status.HTTP_429_TOO_MANY_REQUESTS,
            message="Too many requests",
            errors={"type": "TOO_MANY_REQUESTS", "detail": str(exc.detail)},
        ),
    )
