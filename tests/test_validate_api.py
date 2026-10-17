"""Integration tests for POST /claims/{id}/validate against real Postgres + seed data."""

import uuid

from sqlalchemy import select

from claimflow.db import SessionLocal
from claimflow.models import AuditEvent


def submit(client, payload) -> str:
    return client.post("/claims", json=payload).json()["id"]


def test_clean_claim_becomes_validated(client, claim_payload):
    claim_id = submit(client, claim_payload)

    resp = client.post(f"/claims/{claim_id}/validate")

    assert resp.status_code == 200
    assert resp.json()["status"] == "VALIDATED"
    assert resp.json()["validation_issues"] == []


def test_bad_claim_is_rejected_with_all_reasons_persisted(client, claim_payload):
    claim_payload["member_id"] = "M9999"
    claim_payload["lines"][1]["procedure_code"] = "00000"
    claim_payload["total_billed"] = "999.00"
    claim_id = submit(client, claim_payload)

    client.post(f"/claims/{claim_id}/validate")
    body = client.get(f"/claims/{claim_id}").json()  # re-read: proves it was stored

    assert body["status"] == "REJECTED"
    assert [(i["edit_code"], i["carc"], i["line_number"]) for i in body["validation_issues"]] == [
        ("MEMBER_NOT_FOUND", "31", None),
        ("INVALID_CPT", "181", 2),
        ("TOTAL_MISMATCH", "16", None),
    ]


def test_retired_code_is_invalid_on_a_later_date_of_service(client, claim_payload):
    # M54.5 was retired 2021-09-30; the claim's date of service is recent.
    claim_payload["diagnosis_codes"] = ["M54.5"]
    claim_id = submit(client, claim_payload)

    body = client.post(f"/claims/{claim_id}/validate").json()

    assert body["status"] == "REJECTED"
    assert [i["edit_code"] for i in body["validation_issues"]] == ["INVALID_ICD"]


def test_validation_writes_audit_event_with_transition(client, claim_payload):
    claim_payload["member_id"] = "M9999"
    claim_id = submit(client, claim_payload)
    client.post(f"/claims/{claim_id}/validate")

    with SessionLocal() as session:
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.claim_id == uuid.UUID(claim_id))
            .order_by(AuditEvent.id)
        ).all()

    assert [(e.event_type, e.from_status, e.to_status) for e in events] == [
        ("CLAIM_RECEIVED", None, "RECEIVED"),
        ("CLAIM_REJECTED", "RECEIVED", "REJECTED"),
    ]
    assert events[1].details["issues"][0]["edit_code"] == "MEMBER_NOT_FOUND"


def test_validating_twice_is_409(client, claim_payload):
    claim_id = submit(client, claim_payload)
    client.post(f"/claims/{claim_id}/validate")

    resp = client.post(f"/claims/{claim_id}/validate")

    assert resp.status_code == 409
    assert "VALIDATED" in resp.json()["detail"]


def test_validate_unknown_claim_is_404(client):
    assert client.post(f"/claims/{uuid.uuid4()}/validate").status_code == 404
