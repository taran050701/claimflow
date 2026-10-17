"""Structured logs, request ids, and Prometheus metrics."""

import json
import re
import uuid

import pytest
import structlog
from prometheus_client import REGISTRY
from sqlalchemy import select

from claimflow.db import SessionLocal
from claimflow.logging_config import configure_logging
from claimflow.models import AuditEvent

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture
def logs():
    """Capture log entries (with contextvars merged in, like production)."""
    capture = structlog.testing.LogCapture()
    structlog.configure(processors=[structlog.contextvars.merge_contextvars, capture])
    yield capture.entries
    configure_logging()


def metric(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def process(client, payload, *actions) -> str:
    claim_id = client.post("/claims", json=payload).json()["id"]
    for action in actions:
        assert client.post(f"/claims/{claim_id}/{action}").status_code == 200
    return claim_id


# ---------------------------------------------------------------- request ids and logs


def test_every_response_has_a_request_id(client):
    resp = client.get("/health")
    assert re.fullmatch(r"[0-9a-f]{32}", resp.headers["X-Request-ID"])


def test_caller_supplied_request_id_is_kept(client):
    resp = client.get("/health", headers={"X-Request-ID": "ticket-4711"})
    assert resp.headers["X-Request-ID"] == "ticket-4711"


def test_request_log_line_uses_route_template(client, claim_payload, logs):
    claim_id = client.post("/claims", json=claim_payload).json()["id"]
    resp = client.get(f"/claims/{claim_id}", headers={"X-Request-ID": "req-1"})

    entry = next(e for e in logs if e["event"] == "request" and e["request_id"] == "req-1")
    assert entry["route"] == "/claims/{claim_id}"
    assert entry["path"] == f"/claims/{claim_id}"
    assert entry["status"] == resp.status_code == 200
    assert entry["duration_ms"] >= 0


def test_health_checks_are_not_logged(client, logs):
    client.get("/health")
    assert not [e for e in logs if e["event"] == "request"]


def test_adjudication_log_joins_to_request_and_audit(client, claim_payload, logs):
    claim_id = process(client, claim_payload, "validate")
    resp = client.post(f"/claims/{claim_id}/adjudicate", headers={"X-Request-ID": "req-adj"})

    entry = next(e for e in logs if e["event"] == "claim_adjudicated")
    assert entry["request_id"] == "req-adj" == resp.headers["X-Request-ID"]
    assert entry["outcome"] == "ADJUDICATED"
    with SessionLocal() as s:
        audit_correlations = set(
            s.scalars(
                select(AuditEvent.correlation_id).where(
                    AuditEvent.claim_id == uuid.UUID(claim_id),
                    AuditEvent.event_type == "CLAIM_ADJUDICATED",
                )
            )
        )
    assert audit_correlations == {uuid.UUID(entry["correlation_id"])}


def test_json_log_format(capsys):
    configure_logging("INFO", "json")
    structlog.get_logger().info("hello", claim_id="abc")
    structlog.get_logger().debug("hidden")  # below INFO: filtered out

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert (entry["event"], entry["claim_id"], entry["level"]) == ("hello", "abc", "info")
    assert entry["timestamp"].endswith("Z")


# ---------------------------------------------------------------- metrics


def test_metrics_endpoint_is_prometheus_text(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "claimflow_adjudications_total" in resp.text


def test_lifecycle_increments_business_metrics(client, claim_payload):
    before = {
        "received": metric("claimflow_claims_received_total"),
        "validated": metric("claimflow_claims_validated_total", result="VALIDATED"),
        "adjudicated": metric("claimflow_adjudications_total", outcome="ADJUDICATED"),
        "priced_lines": metric(
            "claimflow_line_decisions_total", outcome="APPROVED", decided_by="Pricing"
        ),
        "latency_count": metric("claimflow_adjudication_duration_seconds_count"),
        "paid": metric("claimflow_claims_finalized_total", status="PAID"),
    }

    process(client, claim_payload, "validate", "adjudicate", "finalize")

    assert metric("claimflow_claims_received_total") == before["received"] + 1
    assert metric("claimflow_claims_validated_total", result="VALIDATED") == before["validated"] + 1
    assert (
        metric("claimflow_adjudications_total", outcome="ADJUDICATED") == before["adjudicated"] + 1
    )
    assert (
        metric("claimflow_line_decisions_total", outcome="APPROVED", decided_by="Pricing")
        == before["priced_lines"] + 2
    )
    assert metric("claimflow_adjudication_duration_seconds_count") == before["latency_count"] + 1
    assert metric("claimflow_claims_finalized_total", status="PAID") == before["paid"] + 1


def test_rejections_count_each_failed_edit(client, claim_payload):
    before = metric("claimflow_validation_failures_total", edit_code="MEMBER_NOT_FOUND")
    claim_payload["member_id"] = "M9999"
    process(client, claim_payload, "validate")
    after = metric("claimflow_validation_failures_total", edit_code="MEMBER_NOT_FOUND")
    assert after == before + 1


def test_http_metrics_use_route_template(client, claim_payload):
    claim_id = client.post("/claims", json=claim_payload).json()["id"]
    before = metric(
        "claimflow_http_requests_total", method="GET", route="/claims/{claim_id}", status="200"
    )
    client.get(f"/claims/{claim_id}")
    after = metric(
        "claimflow_http_requests_total", method="GET", route="/claims/{claim_id}", status="200"
    )
    assert after == before + 1


def test_unknown_paths_share_one_label(client):
    before = metric("claimflow_http_requests_total", method="GET", route="unmatched", status="404")
    client.get(f"/no-such-path/{uuid.uuid4()}")
    after = metric("claimflow_http_requests_total", method="GET", route="unmatched", status="404")
    assert after == before + 1


def test_no_ids_ever_leak_into_metric_labels(client, claim_payload):
    """Cardinality guard: ids in labels would create one time series per claim."""
    process(client, claim_payload, "validate", "adjudicate", "finalize")
    client.get(f"/claims/{uuid.uuid4()}")  # a 404 with an id in the path

    assert not UUID_RE.search(client.get("/metrics").text)
