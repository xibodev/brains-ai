import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

logger = logging.getLogger(__name__)


def _error_payload(message: str, error_type: str, code: str | None = None) -> dict:
    return {"error": {"message": message, "type": error_type, "code": code}}


def _http_exception_type(status_code: int) -> str:
    if status_code == 401:
        return "unauthorized_error"
    if 400 <= status_code < 500:
        return "invalid_request_error"
    return "api_error"


class AdminLoginRequired(Exception):
    """Raised by HTML admin auth to trigger a 303 redirect to /admin/login.

    A dedicated exception (instead of `HTTPException(303, …)`) keeps the
    redirect path out of the JSON-error wrapping in
    :func:`register_exception_handlers` and lets us install a single
    handler on every FastAPI app that mounts the admin router.
    """

    def __init__(self, location: str):
        self.location = location
        super().__init__(location)


def register_admin_redirect_handler(app: FastAPI) -> None:
    """Install the AdminLoginRequired → 303 handler on ``app``.

    Safe to call on any FastAPI app that mounts ``/admin/*`` HTML routes,
    even if the app does not also call :func:`register_exception_handlers`.
    """

    @app.exception_handler(AdminLoginRequired)
    async def _handle(_: Request, exc: AdminLoginRequired):
        return RedirectResponse(url=exc.location, status_code=303)


def register_exception_handlers(app: FastAPI) -> None:
    register_admin_redirect_handler(app)

    @app.exception_handler(HTTPException)
    async def handle_http_exception(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(
                str(exc.detail),
                _http_exception_type(exc.status_code),
                str(exc.status_code),
            ),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "location": [str(part) for part in error.get("loc", ())],
                "type": str(error.get("type", "validation_error")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                **_error_payload(
                    "Request validation failed",
                    "invalid_request_error",
                    "validation_error",
                ),
                "details": errors,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception in API request: %s", exc)
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                "Internal server error", "internal_server_error", "internal_error"
            ),
        )
