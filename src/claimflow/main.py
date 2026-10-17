"""Application entry point: `uvicorn claimflow.main:app`."""

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from claimflow.api.claims import router as claims_router
from claimflow.api.stats import router as stats_router
from claimflow.config import get_settings
from claimflow.domain.errors import (
    ClaimNotFoundError,
    IdempotencyKeyConflictError,
    RemittanceNotAvailableError,
)
from claimflow.domain.state_machine import ActionNotAllowedError, IllegalTransitionError
from claimflow.logging_config import configure_logging
from claimflow.middleware import observe_requests


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="ClaimFlow",
        description="Healthcare claims adjudication pipeline.",
        version="0.1.0",
    )
    app.include_router(claims_router)
    app.include_router(stats_router)
    app.middleware("http")(observe_requests)

    # Domain errors -> HTTP status codes, in one place.
    @app.exception_handler(ClaimNotFoundError)
    def _not_found(_: Request, exc: ClaimNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": f"Claim {exc.args[0]} not found"},
        )

    @app.exception_handler(IdempotencyKeyConflictError)
    def _idem_conflict(_: Request, exc: IdempotencyKeyConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "Idempotency-Key was already used with a different request body"},
        )

    # Illegal lifecycle requests are rejected loudly (409), never silently ignored.
    @app.exception_handler(RemittanceNotAvailableError)
    @app.exception_handler(ActionNotAllowedError)
    @app.exception_handler(IllegalTransitionError)
    def _illegal_state(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    def metrics() -> Response:
        """Prometheus scrape endpoint (text exposition format)."""
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
