"""Unit tests for the rules engine. Pure: hand-built contexts, no database.

Pattern: start from a context where everything passes, change ONE fact, and
assert which rule decided and with which reason code.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

from claimflow.domain.adjudication import (
    AdjudicationContext,
    AuthStatus,
    Benefit,
    ClaimOutcome,
    CoverageSpan,
    FeeScheduleEntry,
    LineOutcome,
    PriorAuth,
    Verdict,
    adjudicate,
)
from claimflow.domain.pricing import Accumulators, PlanTerms
from claimflow.domain.validation import ClaimInput, LineInput

DOS = date(2026, 9, 14)

OFFICE_VISIT = LineInput(1, "99213", 1, D("150.00"))
MRI = LineInput(2, "72148", 1, D("1500.00"))

CLAIM = ClaimInput(
    member_id="M1001",
    provider_npi="1234567893",
    date_of_service=DOS,
    received_date=date(2026, 10, 1),
    diagnosis_codes=("M54.50",),
    total_billed=D("1650.00"),
    lines=(OFFICE_VISIT, MRI),
)

CTX = AdjudicationContext(
    claim=CLAIM,
    coverage_spans=(CoverageSpan("PLN-GOLD", date(2026, 1, 1), None),),
    plan_terms=PlanTerms(D("500.00"), D("0.200"), D("4000.00")),
    benefits={
        "99213": Benefit(covered=True, requires_prior_auth=False, copay=D("25.00")),
        "72148": Benefit(covered=True, requires_prior_auth=True, copay=None),
    },
    fee_schedule={
        "99213": FeeScheduleEntry(D("90.00"), max_units_per_day=1),
        "72148": FeeScheduleEntry(D("600.00"), max_units_per_day=1),
    },
    prior_auths=(
        PriorAuth("PA-1", "72148", AuthStatus.APPROVED, date(2026, 1, 1), date(2026, 12, 31)),
    ),
    already_processed=frozenset(),
    accumulators=Accumulators(D("250.00"), D("250.00")),
)


def line(result, number):
    return next(d for d in result.lines if d.line_number == number)


def rules_run_for_line(result, number):
    return [r.rule for r in result.trace if r.line_number == number]


# ---------------------------------------------------------------- happy path


def test_everything_passes_both_lines_approved_and_priced():
    result = adjudicate(CTX)

    assert result.outcome is ClaimOutcome.ADJUDICATED
    visit, mri = line(result, 1), line(result, 2)
    assert (visit.outcome, visit.pricing.paid, visit.pricing.copay) == (
        LineOutcome.APPROVED,
        D("65.00"),
        D("25.00"),
    )
    assert (mri.outcome, mri.pricing.paid, mri.pricing.patient_responsibility) == (
        LineOutcome.APPROVED,
        D("280.00"),
        D("320.00"),
    )
    assert result.accumulators_after == Accumulators(D("500.00"), D("595.00"))


def test_rules_run_in_order_for_each_line():
    result = adjudicate(CTX)
    assert [r.rule for r in result.trace if r.line_number is None] == ["Eligibility"]
    assert rules_run_for_line(result, 2) == [
        "Coverage",
        "CodeEdits",
        "PriorAuth",
        "Duplicate",
        "Pricing",
    ]


def test_deductible_used_by_one_line_is_not_available_to_the_next():
    # Two deductible-based lines: line 1 consumes the remaining $250.
    first = LineInput(1, "72148", 1, D("1500.00"))
    second = LineInput(2, "72148", 1, D("1500.00"))
    ctx = replace(
        CTX,
        claim=replace(CLAIM, lines=(first, second)),
        fee_schedule={"72148": FeeScheduleEntry(D("600.00"), 2)},
    )

    result = adjudicate(ctx)

    assert line(result, 1).pricing.deductible == D("250.00")
    assert line(result, 2).pricing.deductible == D("0.00")


# ---------------------------------------------------------------- eligibility (claim level)


def test_terminated_coverage_denies_every_line_co_27_and_short_circuits():
    ctx = replace(
        CTX,
        coverage_spans=(CoverageSpan("PLN-GOLD", date(2026, 1, 1), date(2026, 6, 30)),),
        plan_terms=None,
    )

    result = adjudicate(ctx)

    assert result.outcome is ClaimOutcome.ADJUDICATED
    assert {(d.outcome, d.carc, d.decided_by) for d in result.lines} == {
        (LineOutcome.DENIED, "27", "Eligibility")
    }
    assert [r.rule for r in result.trace] == ["Eligibility"]  # nothing else ran
    assert result.accumulators_after == CTX.accumulators


def test_coverage_not_started_yet_is_co_26():
    ctx = replace(
        CTX, coverage_spans=(CoverageSpan("PLN-GOLD", date(2026, 10, 1), None),), plan_terms=None
    )
    assert {d.carc for d in adjudicate(ctx).lines} == {"26"}


def test_plan_switch_uses_span_in_effect_on_dos():
    spans = (
        CoverageSpan("PLN-BRONZE", date(2026, 1, 1), date(2026, 6, 30)),
        CoverageSpan("PLN-GOLD", date(2026, 7, 1), None),
    )
    result = adjudicate(replace(CTX, coverage_spans=spans))
    assert result.trace[0].message == f"Covered by PLN-GOLD on {DOS}"


# ---------------------------------------------------------------- line rules


def test_not_covered_is_co_96_and_later_rules_do_not_run():
    benefits = {**CTX.benefits, "72148": Benefit(False, True, None)}
    result = adjudicate(replace(CTX, benefits=benefits))

    mri = line(result, 2)
    assert (mri.outcome, mri.carc, mri.decided_by) == (LineOutcome.DENIED, "96", "Coverage")
    assert rules_run_for_line(result, 2) == ["Coverage"]  # short-circuit
    assert line(result, 1).outcome is LineOutcome.APPROVED  # other line unaffected


def test_procedure_missing_from_plan_benefits_is_not_covered():
    benefits = {"99213": CTX.benefits["99213"]}
    assert line(adjudicate(replace(CTX, benefits=benefits)), 2).carc == "96"


def test_units_over_daily_limit_is_co_151():
    claim = replace(CLAIM, lines=(replace(OFFICE_VISIT, units=3), MRI))
    visit = line(adjudicate(replace(CTX, claim=claim)), 1)
    assert (visit.outcome, visit.carc, visit.decided_by) == (LineOutcome.DENIED, "151", "CodeEdits")


def test_missing_prior_auth_is_co_197():
    mri = line(adjudicate(replace(CTX, prior_auths=())), 2)
    assert (mri.outcome, mri.carc, mri.decided_by) == (LineOutcome.DENIED, "197", "PriorAuth")


def test_denied_or_expired_auth_does_not_count():
    denied = PriorAuth("PA-2", "72148", AuthStatus.DENIED, date(2026, 1, 1), date(2026, 12, 31))
    expired = PriorAuth("PA-3", "72148", AuthStatus.APPROVED, date(2026, 1, 1), date(2026, 8, 31))
    assert line(adjudicate(replace(CTX, prior_auths=(denied,))), 2).carc == "197"
    assert line(adjudicate(replace(CTX, prior_auths=(expired,))), 2).carc == "197"


def test_auth_for_a_different_procedure_does_not_count():
    other = PriorAuth("PA-4", "73721", AuthStatus.APPROVED, date(2026, 1, 1), date(2026, 12, 31))
    assert line(adjudicate(replace(CTX, prior_auths=(other,))), 2).carc == "197"


def test_pending_auth_pends_the_whole_claim_and_spends_nothing():
    pending = PriorAuth("PA-5", "72148", AuthStatus.PENDING, date(2026, 1, 1), date(2026, 12, 31))
    result = adjudicate(replace(CTX, prior_auths=(pending,)))

    assert result.outcome is ClaimOutcome.PENDED
    assert line(result, 2).outcome is LineOutcome.PENDED
    assert result.accumulators_after == CTX.accumulators  # nothing applied while pended


def test_duplicate_of_already_processed_service_is_co_18():
    result = adjudicate(replace(CTX, already_processed=frozenset({"99213"})))
    visit = line(result, 1)
    assert (visit.outcome, visit.carc, visit.decided_by) == (LineOutcome.DENIED, "18", "Duplicate")
    assert line(result, 2).outcome is LineOutcome.APPROVED


def test_covered_but_no_fee_schedule_rate_pends_for_manual_pricing():
    fees = {"72148": CTX.fee_schedule["72148"]}
    result = adjudicate(replace(CTX, fee_schedule=fees))

    visit = line(result, 1)
    assert (visit.outcome, visit.decided_by) == (LineOutcome.PENDED, "Pricing")
    assert result.outcome is ClaimOutcome.PENDED


def test_every_trace_entry_has_a_verdict_and_denials_have_a_reason_code():
    result = adjudicate(replace(CTX, prior_auths=()))
    for r in result.trace:
        assert r.verdict in Verdict
        if r.verdict is Verdict.DENY:
            assert r.carc
