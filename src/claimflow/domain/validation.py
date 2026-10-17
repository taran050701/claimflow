"""Front-end claim edits. Pure Python: no database, no clock, no framework.

The service gathers everything the edits need (ReferenceData) and calls
validate_claim(). Every check runs and every issue is reported, so the provider
can fix all problems in one resubmission. (Contrast: the Phase 3 rules engine
short-circuits on the first failure.)
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from claimflow.domain.npi import is_valid_npi

# Days after the date of service a provider has to submit. Real payers set this
# by contract (commonly 90-365 days); 365 is the Medicare rule.
TIMELY_FILING_DAYS = 365


# ---------------------------------------------------------------- inputs / outputs


@dataclass(frozen=True)
class LineInput:
    line_number: int
    procedure_code: str
    units: int
    billed_amount: Decimal


@dataclass(frozen=True)
class ClaimInput:
    member_id: str
    provider_npi: str
    date_of_service: date
    received_date: date  # injected "today", so results never depend on the real clock
    diagnosis_codes: tuple[str, ...]
    total_billed: Decimal
    lines: tuple[LineInput, ...]


@dataclass(frozen=True)
class ReferenceData:
    """Facts looked up from the database before validation runs."""

    member_date_of_birth: date | None  # None = member not found
    provider_exists: bool
    valid_cpt_codes: frozenset[str]  # submitted CPT codes that were valid on the DOS
    valid_icd_codes: frozenset[str]  # submitted ICD codes that were valid on the DOS


@dataclass(frozen=True)
class ValidationIssue:
    edit_code: str  # our stable identifier, e.g. "INVALID_CPT"
    carc: str  # industry Claim Adjustment Reason Code
    field: str
    message: str
    line_number: int | None = None


Check = Callable[[ClaimInput, ReferenceData], list[ValidationIssue]]


# ---------------------------------------------------------------- the edits
# Each check is small, independent and returns zero or more issues.


def check_member(claim: ClaimInput, ref: ReferenceData) -> list[ValidationIssue]:
    if not claim.member_id.strip():
        return [ValidationIssue("MEMBER_ID_MISSING", "16", "member_id", "Member ID is required")]
    if ref.member_date_of_birth is None:
        return [
            ValidationIssue(
                "MEMBER_NOT_FOUND",
                "31",
                "member_id",
                f"Member {claim.member_id} cannot be identified as our insured",
            )
        ]
    return []


def check_provider(claim: ClaimInput, ref: ReferenceData) -> list[ValidationIssue]:
    if not claim.provider_npi.strip():
        return [ValidationIssue("NPI_MISSING", "206", "provider_npi", "Provider NPI is required")]
    if not is_valid_npi(claim.provider_npi):
        return [
            ValidationIssue(
                "NPI_INVALID_FORMAT",
                "207",
                "provider_npi",
                f"NPI {claim.provider_npi} is not 10 digits with a valid check digit",
            )
        ]
    if not ref.provider_exists:
        return [
            ValidationIssue(
                "NPI_NOT_FOUND",
                "208",
                "provider_npi",
                f"NPI {claim.provider_npi} does not match a known provider",
            )
        ]
    return []


def check_dates(claim: ClaimInput, ref: ReferenceData) -> list[ValidationIssue]:
    dos = claim.date_of_service
    if dos > claim.received_date:
        return [
            ValidationIssue(
                "DOS_IN_FUTURE", "16", "date_of_service", f"Date of service {dos} is in the future"
            )
        ]

    issues = []
    dob = ref.member_date_of_birth
    if dob is not None and dos < dob:
        issues.append(
            ValidationIssue(
                "DOS_BEFORE_BIRTH",
                "16",
                "date_of_service",
                f"Date of service {dos} is before the member's date of birth",
            )
        )
    if (claim.received_date - dos).days > TIMELY_FILING_DAYS:
        issues.append(
            ValidationIssue(
                "TIMELY_FILING_EXCEEDED",
                "29",
                "date_of_service",
                f"Claim received more than {TIMELY_FILING_DAYS} days after the date of service",
            )
        )
    return issues


def check_diagnoses(claim: ClaimInput, ref: ReferenceData) -> list[ValidationIssue]:
    if not claim.diagnosis_codes:
        return [
            ValidationIssue(
                "DIAGNOSIS_MISSING", "16", "diagnosis_codes", "At least one diagnosis is required"
            )
        ]
    return [
        ValidationIssue(
            "INVALID_ICD",
            "146",
            "diagnosis_codes",
            f"Diagnosis {code} is not a valid ICD-10-CM code on {claim.date_of_service}",
        )
        for code in claim.diagnosis_codes
        if code not in ref.valid_icd_codes
    ]


def check_lines(claim: ClaimInput, ref: ReferenceData) -> list[ValidationIssue]:
    if not claim.lines:
        return [ValidationIssue("LINES_MISSING", "16", "lines", "At least one line is required")]

    issues = []
    for line in claim.lines:
        if line.procedure_code not in ref.valid_cpt_codes:
            issues.append(
                ValidationIssue(
                    "INVALID_CPT",
                    "181",
                    "procedure_code",
                    f"Procedure {line.procedure_code} is not a valid CPT code "
                    f"on {claim.date_of_service}",
                    line.line_number,
                )
            )
        if line.billed_amount <= 0:
            issues.append(
                ValidationIssue(
                    "CHARGE_NOT_POSITIVE",
                    "16",
                    "billed_amount",
                    "Line charge must be greater than zero",
                    line.line_number,
                )
            )
    return issues


def check_total(claim: ClaimInput, ref: ReferenceData) -> list[ValidationIssue]:
    line_sum = sum((line.billed_amount for line in claim.lines), Decimal("0"))
    if claim.lines and line_sum != claim.total_billed:
        return [
            ValidationIssue(
                "TOTAL_MISMATCH",
                "16",
                "total_billed",
                f"Total billed {claim.total_billed} does not equal the sum of lines {line_sum}",
            )
        ]
    return []


ALL_CHECKS: tuple[Check, ...] = (
    check_member,
    check_provider,
    check_dates,
    check_diagnoses,
    check_lines,
    check_total,
)


def validate_claim(claim: ClaimInput, ref: ReferenceData) -> list[ValidationIssue]:
    """Run every edit and return all issues. An empty list means the claim is valid."""
    return [issue for check in ALL_CHECKS for issue in check(claim, ref)]
