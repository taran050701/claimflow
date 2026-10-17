"""Integration tests for POST /claims/{id}/adjudicate: HTTP -> service -> engine -> Postgres.

Each test creates the auths/accumulators it relies on, for the date of service's
year, so the tests keep working as the calendar moves on.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from claimflow.db import SessionLocal
from claimflow.domain.adjudication import AuthStatus
from claimflow.models import AuditEvent, MemberAccumulator, PriorAuthorization

DOS = date.today() - timedelta(days=30)


def set_accumulator(member_id: str, deductible_met: str, oop_met: str) -> None:
    with SessionLocal() as s:
        s.merge(
            MemberAccumulator(
                member_id=member_id,
                plan_year=DOS.year,
                deductible_met=Decimal(deductible_met),
                out_of_pocket_met=Decimal(oop_met),
            )
        )
        s.commit()


def get_accumulator(member_id: str) -> MemberAccumulator:
    with SessionLocal() as s:
        return s.get_one(MemberAccumulator, (member_id, DOS.year))


def set_auth(member_id: str, code: str, status: AuthStatus, number: str = "PA-T1") -> None:
    with SessionLocal() as s:
        s.merge(
            PriorAuthorization(
                auth_number=number,
                member_id=member_id,
                procedure_code=code,
                status=status,
                valid_from=DOS - timedelta(days=60),
                valid_to=DOS + timedelta(days=60),
            )
        )
        s.commit()


@pytest.fixture
def mri_payload() -> dict:
    return {
        "member_id": "M1001",
        "provider_npi": "1987654328",
        "date_of_service": DOS.isoformat(),
        "diagnosis_codes": ["M54.50"],
        "total_billed": "1500.00",
        "lines": [{"procedure_code": "72148", "units": 1, "billed_amount": "1500.00"}],
    }


def submit_and_validate(client, payload) -> str:
    claim_id = client.post("/claims", json=payload).json()["id"]
    assert client.post(f"/claims/{claim_id}/validate").json()["status"] == "VALIDATED"
    return claim_id


def adjudicate(client, claim_id) -> dict:
    resp = client.post(f"/claims/{claim_id}/adjudicate")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_mri_example_end_to_end(client, mri_payload):
    set_accumulator("M1001", "250.00", "250.00")
    set_auth("M1001", "72148", AuthStatus.APPROVED)

    body = adjudicate(client, submit_and_validate(client, mri_payload))

    assert body["status"] == "ADJUDICATED"
    adj = body["lines"][0]["adjudication"]
    assert adj["outcome"] == "APPROVED"
    assert (adj["allowed"], adj["paid"], adj["patient_responsibility"]) == (
        "600.00",
        "280.00",
        "320.00",
    )
    acc = get_accumulator("M1001")
    assert (acc.deductible_met, acc.out_of_pocket_met) == (Decimal("500.00"), Decimal("570.00"))


def test_terminated_member_is_denied_co_27(client, claim_payload):
    claim_payload["member_id"] = "M1003"  # coverage ended 2026-06-30
    body = adjudicate(client, submit_and_validate(client, claim_payload))

    assert body["status"] == "ADJUDICATED"
    assert {
        (line["adjudication"]["outcome"], line["adjudication"]["carc"]) for line in body["lines"]
    } == {("DENIED", "27")}


def test_second_identical_claim_is_denied_as_duplicate(client, claim_payload):
    first = adjudicate(client, submit_and_validate(client, claim_payload))
    second = adjudicate(client, submit_and_validate(client, claim_payload))

    assert {line["adjudication"]["outcome"] for line in first["lines"]} == {"APPROVED"}
    assert {
        (line["adjudication"]["outcome"], line["adjudication"]["carc"]) for line in second["lines"]
    } == {("DENIED", "18")}


def test_pending_auth_pends_then_readjudicates_after_approval(client, mri_payload):
    set_accumulator("M1001", "250.00", "250.00")
    set_auth("M1001", "72148", AuthStatus.PENDING)
    claim_id = submit_and_validate(client, mri_payload)

    pended = adjudicate(client, claim_id)
    assert pended["status"] == "PENDED"
    assert get_accumulator("M1001").deductible_met == Decimal("250.00")  # nothing spent

    set_auth("M1001", "72148", AuthStatus.APPROVED)  # the auth gets approved
    done = adjudicate(client, claim_id)

    assert done["status"] == "ADJUDICATED"
    assert done["lines"][0]["adjudication"]["paid"] == "280.00"
    assert get_accumulator("M1001").deductible_met == Decimal("500.00")


def test_cannot_adjudicate_before_validation(client, claim_payload):
    claim_id = client.post("/claims", json=claim_payload).json()["id"]
    resp = client.post(f"/claims/{claim_id}/adjudicate")
    assert resp.status_code == 409
    assert "RECEIVED" in resp.json()["detail"]


def test_cannot_adjudicate_twice(client, claim_payload):
    claim_id = submit_and_validate(client, claim_payload)
    adjudicate(client, claim_id)
    assert client.post(f"/claims/{claim_id}/adjudicate").status_code == 409


def test_audit_event_records_full_rule_trace(client, claim_payload):
    claim_id = submit_and_validate(client, claim_payload)
    adjudicate(client, claim_id)

    with SessionLocal() as s:
        event = s.scalars(
            select(AuditEvent).where(
                AuditEvent.claim_id == uuid.UUID(claim_id),
                AuditEvent.event_type == "CLAIM_ADJUDICATED",
            )
        ).one()

    assert (event.from_status, event.to_status) == ("VALIDATED", "ADJUDICATED")
    rules = [(t["rule"], t["line_number"]) for t in event.details["trace"]]
    assert rules[0] == ("Eligibility", None)
    assert ("Pricing", 1) in rules and ("Pricing", 2) in rules
    assert "accumulators_before" in event.details and "accumulators_after" in event.details
