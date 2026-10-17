"""API request/response models (the HTTP contract).

These enforce *syntax* only: fields present, correct types, sizes that fit the
columns. A bad payload gets HTTP 422 right away and is never stored, which is
what an X12 999 acknowledgment does.

*Content* rules (does this member exist? is 99213 a real CPT code? does the
total match the lines?) are NOT checked here. Those claims are stored and then
rejected with reasons in Phase 2, like an X12 277CA.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from claimflow.domain.enums import ClaimStatus
from claimflow.domain.state_machine import allowed_actions

# Fits NUMERIC(12,2): at most 12 digits, 2 after the decimal point.
MoneyIn = Annotated[Decimal, Field(max_digits=12, decimal_places=2)]


class ClaimLineIn(BaseModel):
    procedure_code: str = Field(max_length=10, examples=["99213"])
    units: int = Field(default=1, ge=1, le=999)
    billed_amount: MoneyIn = Field(examples=["150.00"])


class ClaimIn(BaseModel):
    member_id: str = Field(max_length=20, examples=["M1001"])
    provider_npi: str = Field(max_length=10, examples=["1234567893"])
    date_of_service: date
    diagnosis_codes: list[Annotated[str, Field(max_length=10)]] = Field(
        max_length=12, examples=[["J06.9"]]
    )
    total_billed: MoneyIn = Field(examples=["150.00"])
    lines: list[ClaimLineIn] = Field(max_length=50)


class LineAdjudicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    outcome: str
    decided_by: str
    carc: str | None
    message: str
    allowed: Decimal
    contractual_adjustment: Decimal
    deductible: Decimal
    copay: Decimal
    coinsurance: Decimal
    patient_responsibility: Decimal
    paid: Decimal


class ClaimLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    line_number: int
    procedure_code: str
    units: int
    billed_amount: Decimal
    adjudication: LineAdjudicationOut | None


class ValidationIssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    line_number: int | None
    edit_code: str
    carc: str
    field: str
    message: str


class ClaimOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    member_id: str
    provider_npi: str
    date_of_service: date
    diagnosis_codes: list[str]
    total_billed: Decimal
    status: ClaimStatus
    created_at: datetime
    updated_at: datetime
    lines: list[ClaimLineOut]
    validation_issues: list[ValidationIssueOut]

    @computed_field
    @property
    def allowed_actions(self) -> list[str]:
        """What a client can do next, derived from the state machine."""
        return allowed_actions(self.status)


class ClaimPage(BaseModel):
    items: list[ClaimOut]
    limit: int
    offset: int


class AppealIn(BaseModel):
    reason: str = Field(
        min_length=1, max_length=500, examples=["Retroactive authorization PA-1001 obtained"]
    )


# ---------------------------------------------------------------- remittance


class AdjustmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    group: str = Field(description="CO = provider writes off, PR = patient owes")
    carc: str
    amount: Decimal
    description: str


class RemitLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    line_number: int
    procedure_code: str
    units: int
    outcome: str
    billed: Decimal
    allowed: Decimal
    paid: Decimal
    patient_responsibility: Decimal
    adjustments: list[AdjustmentOut]


class RemitTotalsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    billed: Decimal
    allowed: Decimal
    paid: Decimal
    patient_responsibility: Decimal
    contractual_adjustments: Decimal


class RemittanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    claim_id: uuid.UUID
    claim_status_code: str = Field(description="835 CLP02: 1 = processed as primary, 4 = denied")
    member_id: str
    provider_npi: str
    date_of_service: date
    totals: RemitTotalsOut
    lines: list[RemitLineOut]


# ---------------------------------------------------------------- audit


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    created_at: datetime
    event_type: str
    correlation_id: uuid.UUID | None
    from_status: str | None
    to_status: str | None
    rule_name: str | None
    line_number: int | None
    outcome: str | None
    reason_code: str | None
    details: dict[str, Any]
    prev_hash: str
    hash: str


class AuditVerificationOut(BaseModel):
    claim_id: uuid.UUID
    valid: bool
    event_count: int
    first_invalid_seq: int | None = Field(
        description="First event whose hash, link or sequence doesn't check out; null if valid"
    )


class LineExplanationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    line_number: int
    procedure_code: str
    outcome: str
    decided_by: str
    reason_code: str | None
    reason: str
    message: str
    inputs: dict[str, Any]
    rules_passed: list[str]


class ValidationFailureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    edit_code: str
    line_number: int | None
    reason_code: str | None
    reason: str
    message: str


class ExplanationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    claim_id: uuid.UUID
    status: str
    summary: str
    lines: list[LineExplanationOut]
    validation_failures: list[ValidationFailureOut]
