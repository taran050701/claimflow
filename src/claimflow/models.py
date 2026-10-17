"""SQLAlchemy ORM models: the database schema expressed as Python classes.

Reference data (plans, members, coverages, providers) uses real foreign keys.
Submitted data (claims) deliberately does NOT foreign-key to members/providers:
a claim for an unknown member must still be stored so validation can reject it
with a reason, instead of the insert failing and the claim vanishing.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from claimflow.domain.adjudication import AuthStatus, LineOutcome
from claimflow.domain.enums import ClaimStatus

# Money: exact decimal with 2 places. Never float.
Money = Numeric(12, 2)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- reference data


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)  # e.g. "PLN-GOLD"
    name: Mapped[str] = mapped_column(String(100))
    annual_deductible: Mapped[Decimal] = mapped_column(Money)
    coinsurance_rate: Mapped[Decimal] = mapped_column(Numeric(4, 3))  # 0.200 = 20%
    out_of_pocket_max: Mapped[Decimal] = mapped_column(Money)

    __table_args__ = (
        CheckConstraint("annual_deductible >= 0", name="deductible_non_negative"),
        CheckConstraint("coinsurance_rate BETWEEN 0 AND 1", name="coinsurance_is_fraction"),
        CheckConstraint("out_of_pocket_max >= annual_deductible", name="oop_max_gte_deductible"),
    )


class Member(Base):
    __tablename__ = "members"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)  # e.g. "M1001"
    first_name: Mapped[str] = mapped_column(String(50))
    last_name: Mapped[str] = mapped_column(String(50))
    date_of_birth: Mapped[date] = mapped_column(Date)

    coverages: Mapped[list["MemberCoverage"]] = relationship(back_populates="member")


class MemberCoverage(Base):
    """Effective-dated enrollment: member X was on plan Y from start_date to end_date.

    end_date NULL means "still active". Eligibility asks: which row covers the
    date of service?
    """

    __tablename__ = "member_coverages"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)

    member: Mapped[Member] = relationship(back_populates="coverages")
    plan: Mapped[Plan] = relationship()

    __table_args__ = (
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="valid_date_range"),
        Index("ix_member_coverages_member_start", "member_id", "start_date"),
    )


class Provider(Base):
    __tablename__ = "providers"

    npi: Mapped[str] = mapped_column(String(10), primary_key=True)  # National Provider ID
    name: Mapped[str] = mapped_column(String(100))
    specialty: Mapped[str] = mapped_column(String(50))


class _CodeSetMixin:
    """Shared shape of a code set: a code is valid between two dates.

    Code sets change yearly (ICD-10 every Oct 1), so "is this code valid?" always
    means "was it valid on the date of service?". effective_end NULL = still valid.
    """

    description: Mapped[str] = mapped_column(String(200))
    effective_start: Mapped[date] = mapped_column(Date)
    effective_end: Mapped[date | None] = mapped_column(Date)


class CptCode(_CodeSetMixin, Base):
    __tablename__ = "cpt_codes"

    code: Mapped[str] = mapped_column(String(5), primary_key=True)


class Icd10Code(_CodeSetMixin, Base):
    __tablename__ = "icd10_codes"

    code: Mapped[str] = mapped_column(String(8), primary_key=True)


class PlanBenefit(Base):
    """What a plan covers, per procedure. No row = not covered."""

    __tablename__ = "plan_benefits"

    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), primary_key=True)
    procedure_code: Mapped[str] = mapped_column(ForeignKey("cpt_codes.code"), primary_key=True)
    covered: Mapped[bool] = mapped_column(Boolean)
    requires_prior_auth: Mapped[bool] = mapped_column(Boolean, default=False)
    copay: Mapped[Decimal | None] = mapped_column(Money)  # NULL = deductible/coinsurance

    __table_args__ = (CheckConstraint("copay IS NULL OR copay >= 0", name="copay_non_negative"),)


class FeeScheduleEntry(Base):
    """Contracted rate per unit for a procedure, plus its daily unit limit (MUE)."""

    __tablename__ = "fee_schedule"

    procedure_code: Mapped[str] = mapped_column(ForeignKey("cpt_codes.code"), primary_key=True)
    rate_per_unit: Mapped[Decimal] = mapped_column(Money)
    max_units_per_day: Mapped[int]

    __table_args__ = (
        CheckConstraint("rate_per_unit >= 0", name="rate_non_negative"),
        CheckConstraint("max_units_per_day > 0", name="max_units_positive"),
    )


class PriorAuthorization(Base):
    __tablename__ = "prior_authorizations"

    auth_number: Mapped[str] = mapped_column(String(20), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id"))
    procedure_code: Mapped[str] = mapped_column(ForeignKey("cpt_codes.code"))
    status: Mapped[AuthStatus] = mapped_column(
        Enum(AuthStatus, native_enum=False, create_constraint=True, length=10, name="auth_status")
    )
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date] = mapped_column(Date)

    __table_args__ = (
        CheckConstraint("valid_to >= valid_from", name="auth_valid_range"),
        Index("ix_prior_auths_member_code", "member_id", "procedure_code"),
    )


class MemberAccumulator(Base):
    """Running totals toward the plan year's deductible and out-of-pocket max.

    Shared mutable state: adjudication locks this row (SELECT ... FOR UPDATE)
    so two claims for the same member can't both spend the same deductible.
    """

    __tablename__ = "member_accumulators"

    member_id: Mapped[str] = mapped_column(ForeignKey("members.id"), primary_key=True)
    plan_year: Mapped[int] = mapped_column(primary_key=True)
    deductible_met: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    out_of_pocket_met: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))


# ---------------------------------------------------------------- submitted data


class ClaimStatusTransition(Base):
    """The legal-transition table, as data. The DB trigger from migration 0004
    rejects any status change not listed here. Must match domain TRANSITIONS
    (a test checks)."""

    __tablename__ = "claim_status_transitions"

    from_status: Mapped[str] = mapped_column(String(20), primary_key=True)
    to_status: Mapped[str] = mapped_column(String(20), primary_key=True)


class Claim(Base):
    __tablename__ = "claims"

    # UUID instead of an auto-increment int: not guessable from outside (you can't
    # enumerate /claims/1, /claims/2...) and can be generated without a DB round trip.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # As submitted -- intentionally no foreign keys (see module docstring).
    member_id: Mapped[str] = mapped_column(String(20))
    provider_npi: Mapped[str] = mapped_column(String(10))
    date_of_service: Mapped[date] = mapped_column(Date)
    # Ordered list: the first diagnosis is the primary one. An array keeps the
    # order and avoids a join for a value we always read together with the claim.
    diagnosis_codes: Mapped[list[str]] = mapped_column(ARRAY(String(10)))
    # The provider states the total; Phase 2 checks it equals the sum of the lines.
    total_billed: Mapped[Decimal] = mapped_column(Money)

    status: Mapped[ClaimStatus] = mapped_column(
        Enum(
            ClaimStatus, native_enum=False, create_constraint=True, length=20, name="claim_status"
        ),
        default=ClaimStatus.RECEIVED,
    )

    # Idempotency: a retried POST with the same key returns the original claim.
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    lines: Mapped[list["ClaimLine"]] = relationship(
        back_populates="claim", order_by="ClaimLine.line_number", cascade="all, delete-orphan"
    )
    validation_issues: Mapped[list["ClaimValidationIssue"]] = relationship(
        order_by="ClaimValidationIssue.id"
    )

    __table_args__ = (
        Index("ix_claims_status", "status"),
        # Supports duplicate detection (same member + date) in Phase 3.
        Index("ix_claims_member_dos", "member_id", "date_of_service"),
    )


class ClaimLine(Base):
    __tablename__ = "claim_lines"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claims.id", ondelete="CASCADE"))
    line_number: Mapped[int]
    procedure_code: Mapped[str] = mapped_column(String(10))  # CPT
    units: Mapped[int]
    billed_amount: Mapped[Decimal] = mapped_column(Money)

    claim: Mapped[Claim] = relationship(back_populates="lines")
    adjudication: Mapped["ClaimLineAdjudication | None"] = relationship(
        back_populates="line", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("claim_id", "line_number", name="uq_claim_line_number"),)


class AuditEvent(Base):
    """One row per decision or state change. Made append-only in Phase 6."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claims.id"))
    event_type: Mapped[str] = mapped_column(String(40))
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_audit_events_claim_created", "claim_id", "created_at"),)


class ClaimValidationIssue(Base):
    """One failed front-end edit. A REJECTED claim has one or more of these."""

    __tablename__ = "claim_validation_issues"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claims.id"))
    line_number: Mapped[int | None]  # None = claim-level issue
    edit_code: Mapped[str] = mapped_column(String(40))
    carc: Mapped[str] = mapped_column(String(5))
    field: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_claim_validation_issues_claim", "claim_id"),)


class ClaimLineAdjudication(Base):
    """The engine's decision for one line. Kept apart from claim_lines on purpose:
    claim_lines is what the provider SUBMITTED; this is what the payer DECIDED."""

    __tablename__ = "claim_line_adjudications"

    claim_line_id: Mapped[int] = mapped_column(
        ForeignKey("claim_lines.id", ondelete="CASCADE"), primary_key=True
    )
    outcome: Mapped[LineOutcome] = mapped_column(
        Enum(LineOutcome, native_enum=False, create_constraint=True, length=10, name="line_outcome")
    )
    decided_by: Mapped[str] = mapped_column(String(40))  # rule name
    carc: Mapped[str | None] = mapped_column(String(5))
    message: Mapped[str] = mapped_column(String(300))
    allowed: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    contractual_adjustment: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    deductible: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    copay: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    coinsurance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    patient_responsibility: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    paid: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    line: Mapped[ClaimLine] = relationship(back_populates="adjudication")

    __table_args__ = (
        # The money must always reconcile -- enforced by the database, not just the code.
        CheckConstraint("allowed = paid + patient_responsibility", name="allowed_reconciles"),
        CheckConstraint(
            "patient_responsibility = deductible + copay + coinsurance",
            name="patient_share_reconciles",
        ),
    )
