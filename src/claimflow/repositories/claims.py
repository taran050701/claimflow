"""Repository: the only layer that writes SQL queries for claims.

The service layer asks for things in business terms ("get the claim with this
idempotency key") and does not care how the query is built. That keeps queries
in one place and makes them easy to change.
"""

import uuid

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, selectinload

from claimflow.domain.enums import ClaimStatus
from claimflow.models import AuditEvent, Claim, ClaimLine


def _with_children(stmt: Select) -> Select:
    # selectinload fetches each child collection in ONE extra query
    # (WHERE claim_id IN ...), avoiding the "N+1 queries" problem.
    return stmt.options(
        selectinload(Claim.lines).selectinload(ClaimLine.adjudication),
        selectinload(Claim.validation_issues),
    )


class ClaimRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, claim: Claim) -> None:
        self.session.add(claim)

    def get(self, claim_id: uuid.UUID, *, for_update: bool = False) -> Claim | None:
        """for_update=True locks the claim row (SELECT ... FOR UPDATE) until the
        transaction ends, so concurrent requests changing the same claim run one
        at a time instead of both acting on a stale status."""
        stmt = _with_children(select(Claim).where(Claim.id == claim_id))
        if for_update:
            stmt = stmt.with_for_update(of=Claim)
        return self.session.scalars(stmt).one_or_none()

    def get_by_idempotency_key(self, key: str) -> Claim | None:
        stmt = _with_children(select(Claim).where(Claim.idempotency_key == key))
        return self.session.scalars(stmt).one_or_none()

    def list(
        self,
        *,
        status: ClaimStatus | None = None,
        member_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Claim]:
        stmt = _with_children(select(Claim))
        if status is not None:
            stmt = stmt.where(Claim.status == status)
        if member_id is not None:
            stmt = stmt.where(Claim.member_id == member_id)
        # Stable ordering is required for offset pagination to be correct.
        stmt = stmt.order_by(Claim.created_at.desc(), Claim.id).limit(limit).offset(offset)
        return list(self.session.scalars(stmt))


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, event: AuditEvent) -> None:
        self.session.add(event)
