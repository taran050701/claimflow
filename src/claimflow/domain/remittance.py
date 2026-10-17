"""Remittance advice: what happened to every billed dollar. Pure Python.

Mirrors the X12 835 (ERA):
  claim level   (CLP)  status code, totals
  service line  (SVC)  billed / allowed / paid per procedure
  adjustments   (CAS)  group code + CARC + amount for every unpaid dollar

The golden rule, checked before a remittance is ever returned:
  billed - sum(adjustments) == paid             (per line and per claim)
  patient_responsibility == sum(PR adjustments)
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from claimflow.domain.adjudication import LineOutcome

ZERO = Decimal("0.00")


class GroupCode(StrEnum):
    CO = "CO"  # contractual obligation: provider writes it off
    PR = "PR"  # patient responsibility: provider bills the patient


# Short descriptions of the CARCs we use (the standard list is published by X12).
CARC_DESCRIPTIONS = {
    "1": "Deductible amount",
    "2": "Coinsurance amount",
    "3": "Co-payment amount",
    "18": "Exact duplicate claim/service",
    "26": "Expenses incurred prior to coverage",
    "27": "Expenses incurred after coverage terminated",
    "45": "Charge exceeds fee schedule/maximum allowable",
    "96": "Non-covered charge(s)",
    "151": "Information does not support this many/frequency of services",
    "197": "Precertification/authorization absent",
}


class ClaimStatusCode(StrEnum):
    """835 CLP02 values we use."""

    PROCESSED_AS_PRIMARY = "1"
    DENIED = "4"


# ---------------------------------------------------------------- input


@dataclass(frozen=True)
class LineForRemit:
    """One line's submission + decision, as stored after adjudication."""

    line_number: int
    procedure_code: str
    units: int
    billed: Decimal
    outcome: LineOutcome
    carc: str | None  # denial reason, if denied
    allowed: Decimal
    deductible: Decimal
    copay: Decimal
    coinsurance: Decimal
    patient_responsibility: Decimal
    paid: Decimal


# ---------------------------------------------------------------- output


@dataclass(frozen=True)
class Adjustment:
    group: GroupCode
    carc: str
    amount: Decimal
    description: str


@dataclass(frozen=True)
class RemitLine:
    line_number: int
    procedure_code: str
    units: int
    outcome: LineOutcome
    billed: Decimal
    allowed: Decimal
    paid: Decimal
    patient_responsibility: Decimal
    adjustments: tuple[Adjustment, ...]


@dataclass(frozen=True)
class RemitTotals:
    billed: Decimal
    allowed: Decimal
    paid: Decimal
    patient_responsibility: Decimal
    contractual_adjustments: Decimal  # sum of CO adjustments


@dataclass(frozen=True)
class Remittance:
    claim_status_code: ClaimStatusCode
    member_id: str
    provider_npi: str
    date_of_service: date
    totals: RemitTotals
    lines: tuple[RemitLine, ...]


class RemittanceImbalanceError(Exception):
    """A remittance whose numbers don't add up must never be issued."""


# ---------------------------------------------------------------- builder


def _adj(group: GroupCode, carc: str, amount: Decimal) -> Adjustment:
    return Adjustment(group, carc, amount, CARC_DESCRIPTIONS.get(carc, ""))


def line_adjustments(line: LineForRemit) -> tuple[Adjustment, ...]:
    """Explain every unpaid dollar on the line. Zero-amount adjustments are omitted."""
    if line.outcome is LineOutcome.DENIED:
        # The whole charge is adjusted with the denial reason. All denials are CO here
        # (a simplification: real 835s often send 26/27 as PR).
        return (_adj(GroupCode.CO, line.carc or "", line.billed),)

    candidates = (
        (GroupCode.CO, "45", line.billed - line.allowed),
        (GroupCode.PR, "1", line.deductible),
        (GroupCode.PR, "2", line.coinsurance),
        (GroupCode.PR, "3", line.copay),
    )
    return tuple(_adj(g, c, amt) for g, c, amt in candidates if amt != ZERO)


def build_remittance(
    *,
    member_id: str,
    provider_npi: str,
    date_of_service: date,
    lines: tuple[LineForRemit, ...],
) -> Remittance:
    if any(line.outcome is LineOutcome.PENDED for line in lines):
        raise ValueError("A pended claim has no remittance")

    remit_lines = tuple(
        RemitLine(
            line_number=line.line_number,
            procedure_code=line.procedure_code,
            units=line.units,
            outcome=line.outcome,
            billed=line.billed,
            allowed=line.allowed,
            paid=line.paid,
            patient_responsibility=line.patient_responsibility,
            adjustments=line_adjustments(line),
        )
        for line in lines
    )
    for rl in remit_lines:
        _check_balanced(rl)

    all_adjustments = [a for rl in remit_lines for a in rl.adjustments]
    totals = RemitTotals(
        billed=_sum(rl.billed for rl in remit_lines),
        allowed=_sum(rl.allowed for rl in remit_lines),
        paid=_sum(rl.paid for rl in remit_lines),
        patient_responsibility=_sum(rl.patient_responsibility for rl in remit_lines),
        contractual_adjustments=_sum(a.amount for a in all_adjustments if a.group is GroupCode.CO),
    )
    if totals.billed - _sum(a.amount for a in all_adjustments) != totals.paid:
        raise RemittanceImbalanceError("Claim totals do not balance")

    approved = any(rl.outcome is LineOutcome.APPROVED for rl in remit_lines)
    return Remittance(
        claim_status_code=(
            ClaimStatusCode.PROCESSED_AS_PRIMARY if approved else ClaimStatusCode.DENIED
        ),
        member_id=member_id,
        provider_npi=provider_npi,
        date_of_service=date_of_service,
        totals=totals,
        lines=remit_lines,
    )


def _check_balanced(line: RemitLine) -> None:
    adjusted = _sum(a.amount for a in line.adjustments)
    patient = _sum(a.amount for a in line.adjustments if a.group is GroupCode.PR)
    if line.billed - adjusted != line.paid:
        raise RemittanceImbalanceError(
            f"Line {line.line_number}: billed {line.billed} - adjustments {adjusted} "
            f"!= paid {line.paid}"
        )
    if patient != line.patient_responsibility:
        raise RemittanceImbalanceError(
            f"Line {line.line_number}: PR adjustments {patient} "
            f"!= patient responsibility {line.patient_responsibility}"
        )


def _sum(values) -> Decimal:
    return sum(values, ZERO)
