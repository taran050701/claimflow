"""Integration tests for claim ingestion: HTTP in, real Postgres underneath."""

import uuid

from sqlalchemy import select

from claimflow.db import SessionLocal
from claimflow.models import AuditEvent


def test_submit_claim_stores_it_as_received(client, claim_payload):
    resp = client.post("/claims", json=claim_payload)

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "RECEIVED"
    assert body["total_billed"] == "250.00"
    assert [line["line_number"] for line in body["lines"]] == [1, 2]

    fetched = client.get(f"/claims/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_submit_writes_received_audit_event_in_same_transaction(client, claim_payload):
    claim_id = client.post("/claims", json=claim_payload).json()["id"]

    with SessionLocal() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.claim_id == uuid.UUID(claim_id))
        ).all()

    assert len(events) == 1
    assert events[0].event_type == "CLAIM_RECEIVED"
    assert events[0].from_status is None
    assert events[0].to_status == "RECEIVED"


def test_unknown_member_is_still_accepted(client, claim_payload):
    """Content problems are validation's job (Phase 2), not ingestion's.
    The claim must be stored so it can be rejected *with a reason*."""
    claim_payload["member_id"] = "M9999"

    resp = client.post("/claims", json=claim_payload)

    assert resp.status_code == 201
    assert resp.json()["status"] == "RECEIVED"


def test_malformed_payload_is_rejected_with_422(client, claim_payload):
    claim_payload["date_of_service"] = "not-a-date"
    claim_payload["lines"][0]["billed_amount"] = "12.345"  # 3 decimal places

    resp = client.post("/claims", json=claim_payload)

    assert resp.status_code == 422
    bad_fields = {tuple(err["loc"]) for err in resp.json()["detail"]}
    assert ("body", "date_of_service") in bad_fields
    assert ("body", "lines", 0, "billed_amount") in bad_fields


def test_retry_with_same_idempotency_key_returns_original_claim(client, claim_payload):
    headers = {"Idempotency-Key": "abc-123"}

    first = client.post("/claims", json=claim_payload, headers=headers)
    retry = client.post("/claims", json=claim_payload, headers=headers)

    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.json()["id"] == first.json()["id"]
    assert len(client.get("/claims").json()["items"]) == 1


def test_same_idempotency_key_with_different_body_is_409(client, claim_payload):
    headers = {"Idempotency-Key": "abc-123"}
    client.post("/claims", json=claim_payload, headers=headers)

    claim_payload["total_billed"] = "999.00"
    resp = client.post("/claims", json=claim_payload, headers=headers)

    assert resp.status_code == 409


def test_without_idempotency_key_each_post_creates_a_claim(client, claim_payload):
    client.post("/claims", json=claim_payload)
    client.post("/claims", json=claim_payload)

    assert len(client.get("/claims").json()["items"]) == 2


def test_get_unknown_claim_is_404(client):
    resp = client.get(f"/claims/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_list_filters_by_member_and_status(client, claim_payload):
    client.post("/claims", json=claim_payload)
    claim_payload["member_id"] = "M1002"
    client.post("/claims", json=claim_payload)

    by_member = client.get("/claims", params={"member_id": "M1002"}).json()["items"]
    assert [c["member_id"] for c in by_member] == ["M1002"]

    received = client.get("/claims", params={"status": "RECEIVED"}).json()["items"]
    assert len(received) == 2
    assert client.get("/claims", params={"status": "PAID"}).json()["items"] == []


def test_idempotency_race_loser_returns_winners_claim(claim_payload, monkeypatch):
    """Simulate two concurrent requests with the same key that BOTH passed the
    "does this key exist?" lookup before either committed. The unique constraint
    makes the second INSERT fail; the service must recover and return the
    winner's claim instead of a 500."""
    from claimflow.repositories.claims import ClaimRepository
    from claimflow.schemas import ClaimIn
    from claimflow.services.claim_service import ClaimService

    claim_in = ClaimIn.model_validate(claim_payload)
    with SessionLocal() as session:
        winner, created = ClaimService(session).submit(claim_in, "race-key")
        assert created

    real_lookup = ClaimRepository.get_by_idempotency_key
    calls = []

    def lookup_misses_first_time(self, key):
        calls.append(key)
        return None if len(calls) == 1 else real_lookup(self, key)

    monkeypatch.setattr(ClaimRepository, "get_by_idempotency_key", lookup_misses_first_time)

    with SessionLocal() as session:
        loser, created = ClaimService(session).submit(claim_in, "race-key")

    assert not created
    assert loser.id == winner.id
