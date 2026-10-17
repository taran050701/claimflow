"""Prometheus metrics, exposed at GET /metrics.

Label rule: only small, fixed sets of values (outcome, rule, route template).
NEVER claim_id / member_id: every distinct label value creates a new time series,
and per-claim labels would explode the metrics store ("cardinality explosion").

Business rates are derived when querying, e.g. claim denial rate:
    sum(claimflow_claims_finalized_total{status="DENIED"})
      / sum(claimflow_claims_finalized_total)
"""

from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter(
    "claimflow_http_requests_total", "HTTP requests", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "claimflow_http_request_duration_seconds", "HTTP request latency", ["method", "route"]
)

CLAIMS_RECEIVED = Counter("claimflow_claims_received_total", "New claims stored")
CLAIMS_VALIDATED = Counter(
    "claimflow_claims_validated_total",
    "Validation results",
    ["result"],  # VALIDATED/REJECTED
)
VALIDATION_FAILURES = Counter(
    "claimflow_validation_failures_total", "Failed validation edits", ["edit_code"]
)
ADJUDICATIONS = Counter(
    "claimflow_adjudications_total",
    "Adjudication runs",
    ["outcome"],  # ADJUDICATED/PENDED
)
LINE_DECISIONS = Counter(
    "claimflow_line_decisions_total", "Line decisions", ["outcome", "decided_by"]
)
ADJUDICATION_LATENCY = Histogram(
    "claimflow_adjudication_duration_seconds",
    "Time to load context, run the rules engine and record results",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
CLAIMS_FINALIZED = Counter(
    "claimflow_claims_finalized_total",
    "Claims finalized",
    ["status"],  # PAID/DENIED
)
APPEALS = Counter("claimflow_appeals_total", "Appeals filed")
