"""Loads everything the rules engine needs and converts rows into domain objects.

This is the "adapter" in ports-and-adapters: the engine only ever sees plain
dataclasses, never SQLAlchemy models.
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from claimflow.domain.adjudication import (
    Benefit,
    CoverageSpan,
    FeeScheduleEntry,
    LineOutcome,
    PriorAuth,
)
from claimflow.domain.pricing import PlanTerms
from claimflow.models import (
    Claim,
    ClaimLine,
    ClaimLineAdjudication,
    MemberAccumulator,
    MemberCoverage,
    Plan,
    PlanBenefit,
    PriorAuthorization,
)
from claimflow.models import FeeScheduleEntry as FeeRow


class AdjudicationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock_accumulator(self, member_id: str, plan_year: int) -> MemberAccumulator:
        """Get-or-create the member's accumulator row and lock it until commit.

        Every adjudication for this member takes this lock first, so adjudications
        for the same member run one at a time. That protects the deductible math
        AND makes the duplicate check race-free.

        INSERT ... ON CONFLICT DO NOTHING handles "two requests create the row at
        once" without an error; the SELECT ... FOR UPDATE then locks whichever row exists.
        """
        self.session.execute(
            insert(MemberAccumulator)
            .values(
                member_id=member_id,
                plan_year=plan_year,
                deductible_met=Decimal("0"),
                out_of_pocket_met=Decimal("0"),
            )
            .on_conflict_do_nothing()
        )
        stmt = (
            select(MemberAccumulator)
            .where(
                MemberAccumulator.member_id == member_id, MemberAccumulator.plan_year == plan_year
            )
            .with_for_update()
        )
        return self.session.scalars(stmt).one()

    def coverage_spans(self, member_id: str) -> tuple[CoverageSpan, ...]:
        rows = self.session.scalars(
            select(MemberCoverage)
            .where(MemberCoverage.member_id == member_id)
            .order_by(MemberCoverage.start_date)
        )
        return tuple(CoverageSpan(r.plan_id, r.start_date, r.end_date) for r in rows)

    def plan_terms(self, plan_id: str) -> PlanTerms:
        plan = self.session.get_one(Plan, plan_id)
        return PlanTerms(plan.annual_deductible, plan.coinsurance_rate, plan.out_of_pocket_max)

    def benefits(self, plan_id: str, codes: set[str]) -> dict[str, Benefit]:
        rows = self.session.scalars(
            select(PlanBenefit).where(
                PlanBenefit.plan_id == plan_id, PlanBenefit.procedure_code.in_(codes)
            )
        )
        return {r.procedure_code: Benefit(r.covered, r.requires_prior_auth, r.copay) for r in rows}

    def fee_schedule(self, codes: set[str]) -> dict[str, FeeScheduleEntry]:
        rows = self.session.scalars(select(FeeRow).where(FeeRow.procedure_code.in_(codes)))
        return {
            r.procedure_code: FeeScheduleEntry(r.rate_per_unit, r.max_units_per_day) for r in rows
        }

    def prior_auths(self, member_id: str, codes: set[str]) -> tuple[PriorAuth, ...]:
        rows = self.session.scalars(
            select(PriorAuthorization).where(
                PriorAuthorization.member_id == member_id,
                PriorAuthorization.procedure_code.in_(codes),
            )
        )
        return tuple(
            PriorAuth(r.auth_number, r.procedure_code, r.status, r.valid_from, r.valid_to)
            for r in rows
        )

    def already_processed(
        self, member_id: str, date_of_service: date, exclude_claim_id: uuid.UUID
    ) -> frozenset[str]:
        """CPT codes approved or pended for this member + date on OTHER claims.

        Denied lines don't count: a corrected resubmission of a denied service
        is not a duplicate payment risk.
        """
        stmt = (
            select(ClaimLine.procedure_code)
            .join(Claim, Claim.id == ClaimLine.claim_id)
            .join(ClaimLineAdjudication, ClaimLineAdjudication.claim_line_id == ClaimLine.id)
            .where(
                Claim.member_id == member_id,
                Claim.date_of_service == date_of_service,
                Claim.id != exclude_claim_id,
                ClaimLineAdjudication.outcome.in_([LineOutcome.APPROVED, LineOutcome.PENDED]),
            )
        )
        return frozenset(self.session.scalars(stmt))
