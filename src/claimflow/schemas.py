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
from typing import Annotated

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
