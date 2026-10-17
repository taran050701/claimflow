"""Unit tests for the remittance builder. Pure: hand-built line decisions."""

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from claimflow.domain.adjudication import LineOutcome
from claimflow.domain.remittance import (
    ClaimStatusCode,
    GroupCode,
    LineForRemit,
    RemittanceImbalanceError,
    build_remittance,
)

Z = D("0.00")

MRI = LineForRemit(  # the Phase 0 example: $250 deductible left, 20% coinsurance
    line_number=1,
    procedure_code="72148",
    units=1,
    billed=D("1500.00"),
    outcome=LineOutcome.APPROVED,
    carc=None,
    allowed=D("600.00"),
    deductible=D("250.00"),
    copay=Z,
    coinsurance=D("70.00"),
    patient_responsibility=D("320.00"),
    paid=D("280.00"),
)
OFFICE_VISIT = LineForRemit(  # $25 copay
    line_number=2,
    procedure_code="99213",
    units=1,
    billed=D("150.00"),
    outcome=LineOutcome.APPROVED,
    carc=None,
    allowed=D("90.00"),
    deductible=Z,
    copay=D("25.00"),
    coinsurance=Z,
    patient_responsibility=D("25.00"),
    paid=D("65.00"),
)
NOT_COVERED = LineForRemit(
    line_number=3,
    procedure_code="97110",
    units=1,
    billed=D("80.00"),
    outcome=LineOutcome.DENIED,
    carc="96",
    allowed=Z,
    deductible=Z,
    copay=Z,
    coinsurance=Z,
    patient_responsibility=Z,
    paid=Z,
)


def remit(*lines):
    return build_remittance(
        member_id="M1001", provider_npi="1987654328", date_of_service=date(2026, 9, 14), lines=lines
    )


def adjustments(remit_line):
    return [(a.group, a.carc, a.amount) for a in remit_line.adjustments]


def test_mri_example_explains_every_dollar():
    r = remit(MRI)
    assert adjustments(r.lines[0]) == [
        (GroupCode.CO, "45", D("900.00")),  # provider writes off
        (GroupCode.PR, "1", D("250.00")),  # deductible
        (GroupCode.PR, "2", D("70.00")),  # coinsurance
    ]
    assert r.totals.paid == D("280.00")
    assert r.totals.patient_responsibility == D("320.00")
    assert r.totals.contractual_adjustments == D("900.00")
    assert r.claim_status_code is ClaimStatusCode.PROCESSED_AS_PRIMARY


def test_copay_is_pr_3():
    assert adjustments(remit(OFFICE_VISIT).lines[0]) == [
        (GroupCode.CO, "45", D("60.00")),
        (GroupCode.PR, "3", D("25.00")),
    ]


def test_denied_line_adjusts_full_charge_with_denial_reason():
    r = remit(NOT_COVERED)
    assert adjustments(r.lines[0]) == [(GroupCode.CO, "96", D("80.00"))]
    assert r.lines[0].adjustments[0].description == "Non-covered charge(s)"
    assert r.claim_status_code is ClaimStatusCode.DENIED


def test_zero_amount_adjustments_are_omitted():
    # Billed exactly the fee schedule, deductible met, no copay -> only coinsurance.
    line = replace(
        MRI,
        billed=D("600.00"),
        deductible=Z,
        coinsurance=D("120.00"),
        patient_responsibility=D("120.00"),
        paid=D("480.00"),
    )
    assert adjustments(remit(line).lines[0]) == [(GroupCode.PR, "2", D("120.00"))]


def test_paid_at_zero_still_processed_not_denied():
    line = replace(
        MRI,
        billed=D("600.00"),
        deductible=D("600.00"),
        coinsurance=Z,
        patient_responsibility=D("600.00"),
        paid=Z,
    )
    r = remit(line)
    assert r.claim_status_code is ClaimStatusCode.PROCESSED_AS_PRIMARY
    assert adjustments(r.lines[0]) == [(GroupCode.PR, "1", D("600.00"))]


def test_mixed_claim_totals_balance():
    r = remit(MRI, OFFICE_VISIT, NOT_COVERED)
    t = r.totals
    all_adj = sum(a.amount for line in r.lines for a in line.adjustments)

    assert t.billed == D("1730.00")
    assert t.paid == D("345.00")
    assert t.patient_responsibility == D("345.00")
    assert t.billed - all_adj == t.paid  # the golden rule
    assert r.claim_status_code is ClaimStatusCode.PROCESSED_AS_PRIMARY  # one approved line


def test_unbalanced_line_is_refused():
    broken = replace(MRI, paid=D("281.00"))  # $1 more paid than the adjustments allow
    with pytest.raises(RemittanceImbalanceError, match="Line 1"):
        remit(broken)


def test_patient_share_must_match_pr_adjustments():
    # Dollars balance (150 - 85 = 65 paid), but the stated patient share is wrong.
    broken = replace(OFFICE_VISIT, patient_responsibility=D("30.00"))
    with pytest.raises(RemittanceImbalanceError, match="PR adjustments"):
        remit(broken)


def test_pended_lines_have_no_remittance():
    with pytest.raises(ValueError, match="pended"):
        remit(replace(MRI, outcome=LineOutcome.PENDED))
