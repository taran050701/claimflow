"""Application entry point: `uvicorn claimflow.main:app`."""

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
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

API_GUIDE = """
Simulates what a health insurer does with medical claims: **validate** them,
**adjudicate** them through an ordered rules engine, track them through a
**state machine**, issue a **remittance**, and **audit-log** every decision.

### Try it in 60 seconds
1. **`GET /claims`**: the demo claims (paid, denied, pended, rejected, appealed).
2. Copy the `id` of a `DENIED` claim, then **`GET /claims/{claim_id}/explanation`**:
   *why* it was denied, which rule decided, and what that rule looked at.
3. **`GET /claims/{claim_id}/remittance`** on a `PAID` claim: every billed dollar
   explained with industry reason codes (CO-45, PR-1, PR-2…).
4. **`GET /claims/{claim_id}/audit/verify`**: proves the hash-chained audit trail
   hasn't been tampered with.
5. **`GET /stats`**: denial rate, top denial reasons, adjudication latency.

Submit your own: `POST /claims` → `/validate` → `/adjudicate` → `/finalize`.
Each claim response lists its `allowed_actions`.

*Free hosting: the first request after idle can take ~30–60 s while the server wakes up.*
"""


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="ClaimFlow",
        description=API_GUIDE,
        version="1.0.0",
        openapi_tags=[
            {"name": "claims", "description": "Submit claims and move them through the lifecycle"},
            {"name": "ops", "description": "Business stats, metrics and health"},
        ],
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

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    def metrics() -> Response:
        """Prometheus scrape endpoint (text exposition format)."""
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
