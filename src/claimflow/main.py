"""Application entry point: `uvicorn claimflow.main:app`."""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from claimflow.api.claims import router as claims_router
from claimflow.domain.errors import ClaimNotFoundError, IdempotencyKeyConflictError
from claimflow.domain.state_machine import ActionNotAllowedError, IllegalTransitionError


def create_app() -> FastAPI:
    app = FastAPI(
        title="ClaimFlow",
        description="Healthcare claims adjudication pipeline.",
        version="0.1.0",
    )
    app.include_router(claims_router)

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
    @app.exception_handler(ActionNotAllowedError)
    @app.exception_handler(IllegalTransitionError)
    def _illegal_state(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
