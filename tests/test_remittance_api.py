"""Integration tests for GET /claims/{id}/remittance."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from claimflow.db import SessionLocal
from claimflow.domain.adjudication import AuthStatus
from claimflow.models import MemberAccumulator, PriorAuthorization

DOS = date.today() - timedelta(days=30)


def act(client, claim_id, action):
    resp = client.post(f"/claims/{claim_id}/{action}")
    assert resp.status_code == 200, resp.text


def finalized(client, payload) -> str:
    claim_id = client.post("/claims", json=payload).json()["id"]
    for action in ("validate", "adjudicate", "finalize"):
        act(client, claim_id, action)
    return claim_id


def adjustments(line):
    return [(a["group"], a["carc"], a["amount"]) for a in line["adjustments"]]


def mri_payload():
    return {
        "member_id": "M1001",
        "provider_npi": "1987654328",
        "date_of_service": DOS.isoformat(),
        "diagnosis_codes": ["M54.50"],
        "total_billed": "1500.00",
        "lines": [{"procedure_code": "72148", "units": 1, "billed_amount": "1500.00"}],
    }


def test_mri_remittance_end_to_end(client):
    with SessionLocal() as s:
        s.merge(
            MemberAccumulator(
                member_id="M1001",
                plan_year=DOS.year,
                deductible_met=Decimal("250.00"),
                out_of_pocket_met=Decimal("250.00"),
            )
        )
        s.merge(
            PriorAuthorization(
                auth_number="PA-R1",
                member_id="M1001",
                procedure_code="72148",
                status=AuthStatus.APPROVED,
                valid_from=DOS,
                valid_to=DOS,
            )
        )
        s.commit()
    claim_id = finalized(client, mri_payload())

    resp = client.get(f"/claims/{claim_id}/remittance")

    assert resp.status_code == 200
    body = resp.json()
    assert body["claim_id"] == claim_id
    assert body["claim_status_code"] == "1"
    assert body["totals"] == {
        "billed": "1500.00",
        "allowed": "600.00",
        "paid": "280.00",
        "patient_responsibility": "320.00",
        "contractual_adjustments": "900.00",
    }
    assert adjustments(body["lines"][0]) == [
        ("CO", "45", "900.00"),
        ("PR", "1", "250.00"),
        ("PR", "2", "70.00"),
    ]


def test_denied_claim_remittance_shows_denial_reason(client):
    claim_id = finalized(client, mri_payload())  # no auth on file

    body = client.get(f"/claims/{claim_id}/remittance").json()

    assert body["claim_status_code"] == "4"
    assert body["totals"]["paid"] == "0.00"
    assert adjustments(body["lines"][0]) == [("CO", "197", "1500.00")]
    assert body["lines"][0]["adjustments"][0]["description"] == (
        "Precertification/authorization absent"
    )


def test_every_remittance_balances(client, claim_payload):
    body = client.get(f"/claims/{finalized(client, claim_payload)}/remittance").json()

    for line in body["lines"]:
        adjusted = sum(Decimal(a["amount"]) for a in line["adjustments"])
        assert Decimal(line["billed"]) - adjusted == Decimal(line["paid"])
        pr = sum(Decimal(a["amount"]) for a in line["adjustments"] if a["group"] == "PR")
        assert pr == Decimal(line["patient_responsibility"])


def test_no_remittance_before_finalization(client, claim_payload):
    claim_id = client.post("/claims", json=claim_payload).json()["id"]
    act(client, claim_id, "validate")
    act(client, claim_id, "adjudicate")  # ADJUDICATED, not yet finalized

    resp = client.get(f"/claims/{claim_id}/remittance")

    assert resp.status_code == 409
    assert "ADJUDICATED" in resp.json()["detail"]


def test_rejected_claim_has_no_remittance(client, claim_payload):
    claim_payload["member_id"] = "M9999"
    claim_id = client.post("/claims", json=claim_payload).json()["id"]
    act(client, claim_id, "validate")

    assert client.get(f"/claims/{claim_id}/remittance").status_code == 409


def test_remittance_for_unknown_claim_is_404(client):
    assert client.get(f"/claims/{uuid.uuid4()}/remittance").status_code == 404
