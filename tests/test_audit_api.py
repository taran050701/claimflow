"""The audit log: append-only, hash-chained, queryable, and able to explain decisions.

Includes "attacks": direct UPDATE/DELETE/TRUNCATE (blocked by triggers), and an
insider who disables the triggers first (caught by the hash chain).
"""

import uuid
from datetime import date, timedelta
from itertools import pairwise

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from claimflow.db import engine

DOS = date.today() - timedelta(days=30)
GENESIS = "0" * 64


def run(client, payload, *actions) -> str:
    claim_id = client.post("/claims", json=payload).json()["id"]
    for action in actions:
        assert client.post(f"/claims/{claim_id}/{action}").status_code == 200
    return claim_id


def mri_payload():
    return {
        "member_id": "M1001",
        "provider_npi": "1987654328",
        "date_of_service": DOS.isoformat(),
        "diagnosis_codes": ["M54.50"],
        "total_billed": "1500.00",
        "lines": [{"procedure_code": "72148", "units": 1, "billed_amount": "1500.00"}],
    }


def as_insider(statements: list[str], params: dict) -> None:
    """Simulate someone with table-owner rights: switch the guard triggers off,
    tamper, switch them back on. Only the hash chain can catch this."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
        for sql in statements:
            conn.execute(text(sql), params)
        conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER USER"))


def verify(client, claim_id) -> dict:
    return client.get(f"/claims/{claim_id}/audit/verify").json()


# ---------------------------------------------------------------- the trail


def test_audit_trail_is_ordered_and_hash_chained(client, claim_payload):
    claim_id = run(client, claim_payload, "validate", "adjudicate", "finalize")

    events = client.get(f"/claims/{claim_id}/audit").json()

    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[0]["prev_hash"] == GENESIS
    for prev, cur in pairwise(events):
        assert cur["prev_hash"] == prev["hash"]
    assert events[-1]["to_status"] == "PAID"
    assert verify(client, claim_id) == {
        "claim_id": claim_id,
        "valid": True,
        "event_count": len(events),
        "first_invalid_seq": None,
    }


def test_rule_rows_record_what_the_rule_looked_at(client):
    claim_id = run(client, mri_payload(), "validate", "adjudicate")

    events = client.get(f"/claims/{claim_id}/audit").json()
    prior_auth = next(e for e in events if e["rule_name"] == "PriorAuth")

    assert (prior_auth["line_number"], prior_auth["outcome"], prior_auth["reason_code"]) == (
        1,
        "DENY",
        "197",
    )
    assert prior_auth["details"]["inputs"] == {
        "required": True,
        "date_of_service": DOS.isoformat(),
        "auths_on_file": [],
    }


def test_audit_for_unknown_claim_is_404(client):
    assert client.get(f"/claims/{uuid.uuid4()}/audit").status_code == 404


# ---------------------------------------------------------------- append-only


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit_events SET details = '{}' WHERE claim_id = :id",
        "DELETE FROM audit_events WHERE claim_id = :id",
        "TRUNCATE audit_events CASCADE",
    ],
)
def test_database_refuses_to_change_audit_events(client, claim_payload, sql):
    claim_id = run(client, claim_payload, "validate")

    with pytest.raises(DBAPIError, match="audit_events is append-only"):
        with engine.begin() as conn:
            conn.execute(text(sql), {"id": claim_id})


# ---------------------------------------------------------------- tamper evidence


def test_edited_event_breaks_the_chain(client):
    claim_id = run(client, mri_payload(), "validate", "adjudicate", "finalize")
    events = client.get(f"/claims/{claim_id}/audit").json()
    target = next(e for e in events if e["rule_name"] == "PriorAuth")

    # The insider rewrites the denial reason to make it look like a coverage issue.
    as_insider(
        ["UPDATE audit_events SET reason_code = '96' WHERE claim_id = :id AND seq = :seq"],
        {"id": claim_id, "seq": target["seq"]},
    )

    result = verify(client, claim_id)
    assert result["valid"] is False
    assert result["first_invalid_seq"] == target["seq"]


def test_deleted_event_breaks_the_chain(client, claim_payload):
    claim_id = run(client, claim_payload, "validate", "adjudicate")

    as_insider(["DELETE FROM audit_events WHERE claim_id = :id AND seq = 3"], {"id": claim_id})

    assert verify(client, claim_id)["first_invalid_seq"] == 3  # gap where seq 3 was


def test_rehashing_one_event_still_breaks_the_next_link(client, claim_payload):
    """A smarter insider recomputes the edited event's own hash. The NEXT event
    still points at the old hash, so the chain still breaks."""
    claim_id = run(client, claim_payload, "validate", "adjudicate")

    as_insider(
        [
            'UPDATE audit_events SET details = \'{"message": "edited"}\' '
            "WHERE claim_id = :id AND seq = 2",
            "UPDATE audit_events SET hash = audit_event_hash(audit_events) "
            "WHERE claim_id = :id AND seq = 2",
        ],
        {"id": claim_id},
    )

    assert verify(client, claim_id)["first_invalid_seq"] == 3


# ---------------------------------------------------------------- explanation


def test_explanation_for_prior_auth_denial(client):
    claim_id = run(client, mri_payload(), "validate", "adjudicate", "finalize")

    body = client.get(f"/claims/{claim_id}/explanation").json()

    assert body["status"] == "DENIED"
    [line] = body["lines"]
    assert (line["procedure_code"], line["outcome"], line["decided_by"], line["reason_code"]) == (
        "72148",
        "DENIED",
        "PriorAuth",
        "197",
    )
    assert line["rules_passed"] == ["Coverage", "CodeEdits"]
    assert line["inputs"]["auths_on_file"] == []
    assert "denied by PriorAuth" in body["summary"]


def test_explanation_for_rejected_claim(client, claim_payload):
    claim_payload["member_id"] = "M9999"
    claim_id = run(client, claim_payload, "validate")

    body = client.get(f"/claims/{claim_id}/explanation").json()

    assert body["status"] == "REJECTED"
    assert [f["edit_code"] for f in body["validation_failures"]] == ["MEMBER_NOT_FOUND"]


def test_explanation_for_paid_claim_shows_pricing(client, claim_payload):
    claim_id = run(client, claim_payload, "validate", "adjudicate", "finalize")

    lines = client.get(f"/claims/{claim_id}/explanation").json()["lines"]

    assert {(li["outcome"], li["decided_by"]) for li in lines} == {("APPROVED", "Pricing")}
    assert lines[0]["inputs"]["result"]["paid"] == "65.00"  # 99213: $90 allowed - $25 copay
