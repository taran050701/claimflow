"""Per-request observability: request id, one structured log line, HTTP metrics."""

import time
import uuid

import structlog
from fastapi import Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from claimflow.metrics import HTTP_LATENCY, HTTP_REQUESTS

log = structlog.get_logger()

# Probed constantly by the host / Prometheus; metrics yes, log lines no.
QUIET_PATHS = {"/health", "/metrics"}


async def observe_requests(request: Request, call_next: RequestResponseEndpoint) -> Response:
    # Honour a caller-supplied id (so a provider's ticket can quote it), else make one.
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("request_failed", method=request.method, path=request.url.path)
        raise
    duration = time.perf_counter() - started

    # The route TEMPLATE ("/claims/{claim_id}"), never the raw path: raw paths contain
    # ids, and every distinct label value is a new time series.
    route = getattr(request.scope.get("route"), "path", "unmatched")
    HTTP_REQUESTS.labels(request.method, route, response.status_code).inc()
    HTTP_LATENCY.labels(request.method, route).observe(duration)
    if request.url.path not in QUIET_PATHS:
        log.info(
            "request",
            method=request.method,
            route=route,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration * 1000, 2),
        )
    response.headers["X-Request-ID"] = request_id
    return response
