"""State machine tests.

1. Properties of the whole graph (reachability, no dead ends, terminal states).
2. Specific legal / illegal examples and the action guard.
3. The database copy of the rules: identical to the domain, and enforced.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from claimflow.db import SessionLocal, engine
from claimflow.domain.enums import ClaimStatus as S
from claimflow.domain.state_machine import (
    INITIAL,
    TERMINAL,
    TRANSITIONS,
    ActionNotAllowedError,
    IllegalTransitionError,
    allowed_actions,
    assert_action_allowed,
    assert_transition,
    can_transition,
)
from claimflow.models import ClaimStatusTransition


def reachable_from(start: S) -> set[S]:
    seen, stack = {start}, [start]
    while stack:
        for nxt in TRANSITIONS[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


# ---------------------------------------------------------------- graph properties


def test_every_status_has_an_entry():
    assert set(TRANSITIONS) == set(S)


def test_every_status_is_reachable_from_received():
    assert reachable_from(INITIAL) == set(S)


def test_terminal_states_are_paid_and_rejected():
    assert TERMINAL == {S.PAID, S.REJECTED}


def test_no_dead_ends_every_status_can_still_finish():
    for status in S:
        assert reachable_from(status) & TERMINAL, f"{status} can never reach a terminal state"


def test_nothing_goes_back_to_received():
    assert all(S.RECEIVED not in targets for targets in TRANSITIONS.values())


# ---------------------------------------------------------------- examples


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.RECEIVED, S.VALIDATED),
        (S.RECEIVED, S.REJECTED),
        (S.VALIDATED, S.PENDED),
        (S.PENDED, S.PENDED),
        (S.ADJUDICATED, S.PAID),
        (S.DENIED, S.APPEALED),
        (S.APPEALED, S.ADJUDICATED),
    ],
)
def test_legal(current, target):
    assert can_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.RECEIVED, S.PAID),  # skipping validation and adjudication
        (S.RECEIVED, S.ADJUDICATED),
        (S.VALIDATED, S.PAID),  # paying without a decision
        (S.REJECTED, S.APPEALED),  # rejections are resubmitted, not appealed
        (S.PAID, S.DENIED),  # terminal
        (S.PAID, S.APPEALED),
        (S.APPEALED, S.PAID),  # appeal must go back through the rules engine
        (S.DENIED, S.PAID),
    ],
)
def test_illegal(current, target):
    assert not can_transition(current, target)
    with pytest.raises(IllegalTransitionError, match=f"{current} -> {target}"):
        assert_transition(current, target)


def test_allowed_actions_per_status():
    assert allowed_actions(S.RECEIVED) == ["validate"]
    assert allowed_actions(S.PENDED) == ["adjudicate"]
    assert allowed_actions(S.ADJUDICATED) == ["finalize"]
    assert allowed_actions(S.DENIED) == ["appeal"]
    assert allowed_actions(S.PAID) == []


def test_action_error_tells_the_client_what_is_allowed():
    with pytest.raises(ActionNotAllowedError) as err:
        assert_action_allowed(S.RECEIVED, "finalize")
    assert str(err.value) == (
        "Cannot finalize a claim in status RECEIVED. Allowed actions: validate"
    )


def test_action_error_on_terminal_status():
    with pytest.raises(ActionNotAllowedError, match="none \\(terminal status\\)"):
        assert_action_allowed(S.PAID, "appeal")


# ---------------------------------------------------------------- database enforcement


def test_database_transition_table_matches_domain():
    """Two copies of the rules (Python + DB) must never drift apart."""
    with SessionLocal() as s:
        db_pairs = {(r.from_status, r.to_status) for r in s.scalars(select(ClaimStatusTransition))}
    domain_pairs = {(f.value, t.value) for f, targets in TRANSITIONS.items() for t in targets}
    assert db_pairs == domain_pairs


def test_database_rejects_illegal_update_even_bypassing_the_app(client, claim_payload):
    claim_id = client.post("/claims", json=claim_payload).json()["id"]

    with pytest.raises(IntegrityError, match="illegal claim status transition RECEIVED -> PAID"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE claims SET status = 'PAID' WHERE id = :id"), {"id": claim_id})


def test_database_rejects_claims_created_in_a_later_status():
    with pytest.raises(IntegrityError, match="must be created in status RECEIVED"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO claims (id, member_id, provider_npi, date_of_service, "
                    "diagnosis_codes, total_billed, status) VALUES "
                    "(:id, 'M1001', '1234567893', CURRENT_DATE, ARRAY['J06.9'], 1, 'PAID')"
                ),
                {"id": uuid.uuid4()},
            )
