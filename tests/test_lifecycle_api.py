"""End-to-end lifecycle tests: finalize, appeals, illegal requests, audit history."""

import uuid
from datetime import date, timedelta

from sqlalchemy import select

from claimflow.db import SessionLocal
from claimflow.domain.adjudication import AuthStatus
from claimflow.models import AuditEvent, PriorAuthorization

DOS = date.today() - timedelta(days=30)


def act(client, claim_id, action, **kwargs) -> dict:
    resp = client.post(f"/claims/{claim_id}/{action}", **kwargs)
    assert resp.status_code == 200, resp.text
    return resp.json()


def process(client, payload) -> tuple[str, dict]:
    """Submit -> validate -> adjudicate -> finalize."""
    claim_id = client.post("/claims", json=payload).json()["id"]
    act(client, claim_id, "validate")
    act(client, claim_id, "adjudicate")
    return claim_id, act(client, claim_id, "finalize")


def approve_auth(member_id: str, code: str) -> None:
    with SessionLocal() as s:
        s.merge(
            PriorAuthorization(
                auth_number="PA-RETRO",
                member_id=member_id,
                procedure_code=code,
                status=AuthStatus.APPROVED,
                valid_from=DOS - timedelta(days=5),
                valid_to=DOS + timedelta(days=5),
            )
        )
        s.commit()


def mri_payload() -> dict:
    return {
        "member_id": "M1001",
        "provider_npi": "1987654328",
        "date_of_service": DOS.isoformat(),
        "diagnosis_codes": ["M54.50"],
        "total_billed": "1500.00",
        "lines": [{"procedure_code": "72148", "units": 1, "billed_amount": "1500.00"}],
    }


def statuses_in_audit_log(claim_id: str) -> list[tuple[str | None, str]]:
    with SessionLocal() as s:
        events = s.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.claim_id == uuid.UUID(claim_id),
                AuditEvent.to_status.is_not(None),  # state changes only, not rule rows
            )
            .order_by(AuditEvent.seq)
        )
        return [(e.from_status, e.to_status) for e in events]


def test_happy_path_ends_paid_with_complete_history(client, claim_payload):
    claim_id, body = process(client, claim_payload)

    assert body["status"] == "PAID"
    assert body["allowed_actions"] == []
    assert statuses_in_audit_log(claim_id) == [
        (None, "RECEIVED"),
        ("RECEIVED", "VALIDATED"),
        ("VALIDATED", "ADJUDICATED"),
        ("ADJUDICATED", "PAID"),
    ]


def test_paid_at_zero_when_everything_goes_to_deductible_is_still_paid(client, claim_payload):
    # A strep test alone: no copay, member has met none of the deductible.
    claim_payload["lines"] = [{"procedure_code": "87880", "units": 1, "billed_amount": "40.00"}]
    claim_payload["total_billed"] = "40.00"
    _, body = process(client, claim_payload)

    assert body["status"] == "PAID"
    assert body["lines"][0]["adjudication"]["paid"] == "0.00"
    assert body["lines"][0]["adjudication"]["deductible"] == "20.00"


def test_all_lines_denied_finalizes_to_denied(client):
    _, body = process(client, mri_payload())  # no auth on file -> CO-197
    assert body["status"] == "DENIED"
    assert body["allowed_actions"] == ["appeal"]


def test_appeal_overturned_after_retro_auth(client):
    claim_id, denied = process(client, mri_payload())
    assert denied["status"] == "DENIED"

    appealed = act(client, claim_id, "appeal", json={"reason": "Retro auth obtained"})
    assert (appealed["status"], appealed["allowed_actions"]) == ("APPEALED", ["adjudicate"])

    approve_auth("M1001", "72148")
    assert act(client, claim_id, "adjudicate")["status"] == "ADJUDICATED"
    final = act(client, claim_id, "finalize")

    assert final["status"] == "PAID"
    assert statuses_in_audit_log(claim_id)[-4:] == [
        ("ADJUDICATED", "DENIED"),
        ("DENIED", "APPEALED"),
        ("APPEALED", "ADJUDICATED"),
        ("ADJUDICATED", "PAID"),
    ]


def test_appeal_upheld_when_rules_still_deny(client):
    claim_id, _ = process(client, mri_payload())
    act(client, claim_id, "appeal", json={"reason": "Please reconsider"})
    act(client, claim_id, "adjudicate")  # still no auth

    assert act(client, claim_id, "finalize")["status"] == "DENIED"


def test_appeal_reason_is_recorded(client):
    claim_id, _ = process(client, mri_payload())
    act(client, claim_id, "appeal", json={"reason": "Retro auth obtained"})

    with SessionLocal() as s:
        event = s.scalars(
            select(AuditEvent).where(
                AuditEvent.claim_id == uuid.UUID(claim_id),
                AuditEvent.event_type == "CLAIM_APPEALED",
            )
        ).one()
    assert event.details == {"reason": "Retro auth obtained"}


def test_appeal_requires_a_reason(client):
    claim_id, _ = process(client, mri_payload())
    assert client.post(f"/claims/{claim_id}/appeal", json={"reason": ""}).status_code == 422


def test_paid_claim_cannot_be_appealed(client, claim_payload):
    claim_id, _ = process(client, claim_payload)

    resp = client.post(f"/claims/{claim_id}/appeal", json={"reason": "x"})

    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "Cannot appeal a claim in status PAID. Allowed actions: none (terminal status)"
    )


def test_rejected_claim_cannot_be_appealed(client, claim_payload):
    claim_payload["member_id"] = "M9999"
    claim_id = client.post("/claims", json=claim_payload).json()["id"]
    act(client, claim_id, "validate")

    assert client.post(f"/claims/{claim_id}/appeal", json={"reason": "x"}).status_code == 409


def test_cannot_finalize_before_adjudication(client, claim_payload):
    claim_id = client.post("/claims", json=claim_payload).json()["id"]

    resp = client.post(f"/claims/{claim_id}/finalize")

    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "Cannot finalize a claim in status RECEIVED. Allowed actions: validate"
    )


def test_illegal_request_changes_nothing(client, claim_payload):
    claim_id = client.post("/claims", json=claim_payload).json()["id"]
    client.post(f"/claims/{claim_id}/finalize")

    assert client.get(f"/claims/{claim_id}").json()["status"] == "RECEIVED"
    assert statuses_in_audit_log(claim_id) == [(None, "RECEIVED")]


def test_new_claim_shows_validate_as_next_action(client, claim_payload):
    body = client.post("/claims", json=claim_payload).json()
    assert body["allowed_actions"] == ["validate"]
