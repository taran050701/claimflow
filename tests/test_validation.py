"""Unit tests for the front-end edits. Pure functions: no database, no clock.
Each test builds a valid claim, breaks one thing, and checks the exact issue."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from claimflow.domain.npi import is_valid_npi
from claimflow.domain.validation import (
    TIMELY_FILING_DAYS,
    ClaimInput,
    LineInput,
    ReferenceData,
    validate_claim,
)

VALID_CLAIM = ClaimInput(
    member_id="M1001",
    provider_npi="1234567893",
    date_of_service=date(2026, 9, 14),
    received_date=date(2026, 10, 1),
    diagnosis_codes=("J06.9",),
    total_billed=Decimal("250.00"),
    lines=(
        LineInput(1, "99213", 1, Decimal("150.00")),
        LineInput(2, "87880", 1, Decimal("100.00")),
    ),
)

VALID_REF = ReferenceData(
    member_date_of_birth=date(1988, 3, 14),
    provider_exists=True,
    valid_cpt_codes=frozenset({"99213", "87880"}),
    valid_icd_codes=frozenset({"J06.9"}),
)


def edit_codes(claim=VALID_CLAIM, ref=VALID_REF) -> list[str]:
    return [issue.edit_code for issue in validate_claim(claim, ref)]


def test_valid_claim_has_no_issues():
    assert validate_claim(VALID_CLAIM, VALID_REF) == []


# ---------------------------------------------------------------- member / provider


def test_blank_member_id():
    assert edit_codes(replace(VALID_CLAIM, member_id="  ")) == ["MEMBER_ID_MISSING"]


def test_unknown_member_is_carc_31():
    [issue] = validate_claim(VALID_CLAIM, replace(VALID_REF, member_date_of_birth=None))
    assert (issue.edit_code, issue.carc) == ("MEMBER_NOT_FOUND", "31")


def test_bad_npi_check_digit_is_reported_as_format_not_lookup():
    # Last digit changed: fails the Luhn check, so we don't also say "not found".
    claim = replace(VALID_CLAIM, provider_npi="1234567890")
    assert edit_codes(claim, replace(VALID_REF, provider_exists=False)) == ["NPI_INVALID_FORMAT"]


def test_well_formed_but_unknown_npi():
    assert edit_codes(ref=replace(VALID_REF, provider_exists=False)) == ["NPI_NOT_FOUND"]


@pytest.mark.parametrize(
    ("npi", "valid"),
    [
        ("1234567893", True),  # CMS's published example NPI
        ("1987654328", True),
        ("1234567890", False),  # wrong check digit
        ("123456789", False),  # 9 digits
        ("12345678AB", False),  # not numeric
    ],
)
def test_npi_luhn(npi, valid):
    assert is_valid_npi(npi) is valid


# ---------------------------------------------------------------- dates


def test_future_date_of_service():
    claim = replace(VALID_CLAIM, date_of_service=date(2026, 10, 2))
    assert edit_codes(claim) == ["DOS_IN_FUTURE"]


def test_date_of_service_received_same_day_is_fine():
    assert edit_codes(replace(VALID_CLAIM, date_of_service=VALID_CLAIM.received_date)) == []


def test_service_before_member_was_born():
    ref = replace(VALID_REF, member_date_of_birth=date(2026, 9, 20))
    assert edit_codes(ref=ref) == ["DOS_BEFORE_BIRTH"]


def test_timely_filing_boundary():
    received = VALID_CLAIM.received_date
    on_limit = replace(
        VALID_CLAIM, date_of_service=date.fromordinal(received.toordinal() - TIMELY_FILING_DAYS)
    )
    one_day_late = replace(
        VALID_CLAIM, date_of_service=date.fromordinal(received.toordinal() - TIMELY_FILING_DAYS - 1)
    )

    assert edit_codes(on_limit) == []
    assert edit_codes(one_day_late) == ["TIMELY_FILING_EXCEEDED"]


# ---------------------------------------------------------------- codes / lines


def test_no_diagnosis():
    assert edit_codes(replace(VALID_CLAIM, diagnosis_codes=())) == ["DIAGNOSIS_MISSING"]


def test_each_invalid_icd_is_reported():
    claim = replace(VALID_CLAIM, diagnosis_codes=("J06.9", "XXX", "M54.5"))
    issues = validate_claim(claim, VALID_REF)
    assert [(i.edit_code, i.carc) for i in issues] == [("INVALID_ICD", "146")] * 2
    assert "M54.5" in issues[1].message


def test_invalid_cpt_points_at_the_line():
    ref = replace(VALID_REF, valid_cpt_codes=frozenset({"99213"}))
    [issue] = validate_claim(VALID_CLAIM, ref)
    assert (issue.edit_code, issue.carc, issue.line_number) == ("INVALID_CPT", "181", 2)


def test_no_lines():
    claim = replace(VALID_CLAIM, lines=(), total_billed=Decimal("0"))
    assert edit_codes(claim) == ["LINES_MISSING"]


def test_zero_charge_line():
    claim = replace(
        VALID_CLAIM,
        lines=(LineInput(1, "99213", 1, Decimal("0.00")),),
        total_billed=Decimal("0.00"),
    )
    assert edit_codes(claim) == ["CHARGE_NOT_POSITIVE"]


def test_total_must_equal_sum_of_lines():
    assert edit_codes(replace(VALID_CLAIM, total_billed=Decimal("249.99"))) == ["TOTAL_MISMATCH"]


# ---------------------------------------------------------------- collect-all behaviour


def test_reports_every_problem_at_once_not_just_the_first():
    claim = replace(
        VALID_CLAIM,
        provider_npi="0000000000",
        diagnosis_codes=(),
        total_billed=Decimal("1.00"),
    )
    ref = replace(VALID_REF, member_date_of_birth=None)

    assert edit_codes(claim, ref) == [
        "MEMBER_NOT_FOUND",
        "NPI_INVALID_FORMAT",
        "DIAGNOSIS_MISSING",
        "TOTAL_MISMATCH",
    ]
