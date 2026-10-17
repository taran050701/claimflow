"""The adjudication rules engine. Pure Python: no database, no clock, no framework.

Flow:
    1. EligibilityRule (claim level): no coverage on the date of service ->
       every line denied, nothing else runs.
    2. For each line, LINE_RULES in order; the first non-pass result decides
       the line (short-circuit):
         Coverage -> CodeEdits -> PriorAuth -> Duplicate
    3. Lines that pass every rule are priced, threading accumulators so
       line 2 sees the deductible line 1 used.
    4. Any pended line pends the whole claim.

Every rule evaluation is recorded in a trace, so the audit log can say exactly
which rule decided what, and why.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from claimflow.domain.pricing import Accumulators, LinePricing, PlanTerms, price_line
from claimflow.domain.validation import ClaimInput, LineInput

# ---------------------------------------------------------------- reference facts


@dataclass(frozen=True)
class CoverageSpan:
    plan_id: str
    start_date: date
    end_date: date | None  # None = still active

    def covers(self, day: date) -> bool:
        return self.start_date <= day and (self.end_date is None or day <= self.end_date)


def find_coverage(spans: tuple[CoverageSpan, ...], day: date) -> CoverageSpan | None:
    """Which enrollment span (if any) was in effect on this day?"""
    return next((span for span in spans if span.covers(day)), None)


@dataclass(frozen=True)
class Benefit:
    covered: bool
    requires_prior_auth: bool
    copay: Decimal | None  # None = deductible/coinsurance apply instead


@dataclass(frozen=True)
class FeeScheduleEntry:
    rate_per_unit: Decimal
    max_units_per_day: int  # NCCI-style Medically Unlikely Edit


class AuthStatus(StrEnum):
    APPROVED = "APPROVED"
    PENDING = "PENDING"
    DENIED = "DENIED"


@dataclass(frozen=True)
class PriorAuth:
    auth_number: str
    procedure_code: str
    status: AuthStatus
    valid_from: date
    valid_to: date


@dataclass(frozen=True)
class AdjudicationContext:
    """Everything the engine needs, gathered by the service beforehand."""

    claim: ClaimInput
    coverage_spans: tuple[CoverageSpan, ...]
    plan_terms: PlanTerms | None  # terms of the plan in effect on the DOS
    benefits: Mapping[str, Benefit]  # by CPT, for that plan
    fee_schedule: Mapping[str, FeeScheduleEntry]  # by CPT
    prior_auths: tuple[PriorAuth, ...]  # member's auths
    # CPT codes already approved/pended for this member + DOS on OTHER claims.
    already_processed: frozenset[str]
    accumulators: Accumulators


# ---------------------------------------------------------------- results


class Verdict(StrEnum):
    """What a single rule says."""

    PASS = "PASS"
    DENY = "DENY"
    PEND = "PEND"


class LineOutcome(StrEnum):
    """What finally happened to a line. Separate from Verdict on purpose:
    "PASS" is never a valid final state for a line, so it can't be stored as one."""

    APPROVED = "APPROVED"
    DENIED = "DENIED"
    PENDED = "PENDED"


_VERDICT_TO_OUTCOME = {Verdict.DENY: LineOutcome.DENIED, Verdict.PEND: LineOutcome.PENDED}


@dataclass(frozen=True)
class RuleResult:
    rule: str
    verdict: Verdict
    message: str
    carc: str | None = None
    line_number: int | None = None  # None = claim-level rule

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS


@dataclass(frozen=True)
class LineDecision:
    line_number: int
    outcome: LineOutcome
    decided_by: str  # rule name that decided it ("Pricing" for approvals)
    carc: str | None
    message: str
    pricing: LinePricing | None  # only for APPROVED lines


class ClaimOutcome(StrEnum):
    ADJUDICATED = "ADJUDICATED"
    PENDED = "PENDED"


@dataclass(frozen=True)
class AdjudicationResult:
    outcome: ClaimOutcome
    lines: tuple[LineDecision, ...]
    trace: tuple[RuleResult, ...]  # every rule evaluation, in order
    accumulators_after: Accumulators


# ---------------------------------------------------------------- rules


def _pass(rule: str, line: LineInput | None, message: str) -> RuleResult:
    return RuleResult(rule, Verdict.PASS, message, None, line.line_number if line else None)


def _deny(rule: str, line: LineInput | None, carc: str, message: str) -> RuleResult:
    return RuleResult(rule, Verdict.DENY, message, carc, line.line_number if line else None)


def _pend(rule: str, line: LineInput | None, message: str) -> RuleResult:
    return RuleResult(rule, Verdict.PEND, message, None, line.line_number if line else None)


class EligibilityRule:
    """Claim level: was the member covered on the date of service?"""

    name = "Eligibility"

    def evaluate(self, ctx: AdjudicationContext) -> RuleResult:
        dos = ctx.claim.date_of_service
        span = find_coverage(ctx.coverage_spans, dos)
        if span is not None:
            return _pass(self.name, None, f"Covered by {span.plan_id} on {dos}")
        if any(s.end_date is not None and s.end_date < dos for s in ctx.coverage_spans):
            return _deny(self.name, None, "27", f"Coverage terminated before {dos}")
        return _deny(self.name, None, "26", f"Coverage not yet in effect on {dos}")


class LineRule(Protocol):
    name: str

    def evaluate(self, line: LineInput, ctx: AdjudicationContext) -> RuleResult: ...


class CoverageRule:
    """Does the member's plan cover this procedure?"""

    name = "Coverage"

    def evaluate(self, line: LineInput, ctx: AdjudicationContext) -> RuleResult:
        benefit = ctx.benefits.get(line.procedure_code)
        if benefit is None or not benefit.covered:
            return _deny(self.name, line, "96", f"{line.procedure_code} is not a covered benefit")
        return _pass(self.name, line, f"{line.procedure_code} is covered")


class CodeEditsRule:
    """Billing limits on the code: units per day must not exceed the MUE.
    (Whether the code exists was already checked in validation.)"""

    name = "CodeEdits"

    def evaluate(self, line: LineInput, ctx: AdjudicationContext) -> RuleResult:
        fee = ctx.fee_schedule.get(line.procedure_code)
        if fee is not None and line.units > fee.max_units_per_day:
            return _deny(
                self.name,
                line,
                "151",
                f"{line.units} units exceeds the daily limit of {fee.max_units_per_day}",
            )
        return _pass(self.name, line, "Within unit limits")


class PriorAuthRule:
    """If the plan requires prior authorization for this code, was it obtained?"""

    name = "PriorAuth"

    def evaluate(self, line: LineInput, ctx: AdjudicationContext) -> RuleResult:
        benefit = ctx.benefits[line.procedure_code]  # CoverageRule guarantees it exists
        if not benefit.requires_prior_auth:
            return _pass(self.name, line, "No authorization required")

        dos = ctx.claim.date_of_service
        auths = [
            a
            for a in ctx.prior_auths
            if a.procedure_code == line.procedure_code and a.valid_from <= dos <= a.valid_to
        ]
        if any(a.status is AuthStatus.APPROVED for a in auths):
            return _pass(self.name, line, "Approved authorization on file")
        if any(a.status is AuthStatus.PENDING for a in auths):
            return _pend(self.name, line, "Authorization request is still under review")
        return _deny(self.name, line, "197", "Required prior authorization is absent")


class DuplicateRule:
    """Same member + procedure + date of service already processed on another claim."""

    name = "Duplicate"

    def evaluate(self, line: LineInput, ctx: AdjudicationContext) -> RuleResult:
        if line.procedure_code in ctx.already_processed:
            return _deny(
                self.name,
                line,
                "18",
                f"{line.procedure_code} on {ctx.claim.date_of_service} was already processed",
            )
        return _pass(self.name, line, "Not a duplicate")


# Order matters: cheap, decisive checks first; pricing only for lines that pass all.
LINE_RULES: tuple[LineRule, ...] = (
    CoverageRule(),
    CodeEditsRule(),
    PriorAuthRule(),
    DuplicateRule(),
)
PRICING = "Pricing"


# ---------------------------------------------------------------- the engine


def adjudicate(ctx: AdjudicationContext) -> AdjudicationResult:
    trace: list[RuleResult] = []

    eligibility = EligibilityRule().evaluate(ctx)
    trace.append(eligibility)
    if not eligibility.passed:
        all_denied = tuple(
            LineDecision(
                line.line_number,
                LineOutcome.DENIED,
                eligibility.rule,
                eligibility.carc,
                eligibility.message,
                None,
            )
            for line in ctx.claim.lines
        )
        return AdjudicationResult(
            ClaimOutcome.ADJUDICATED, all_denied, tuple(trace), ctx.accumulators
        )

    assert ctx.plan_terms is not None  # eligibility passed, so a plan is in effect
    acc = ctx.accumulators
    decisions: list[LineDecision] = []

    for line in ctx.claim.lines:
        decision = None
        for rule in LINE_RULES:
            result = rule.evaluate(line, ctx)
            trace.append(result)
            if not result.passed:  # short-circuit: first failure decides the line
                decision = LineDecision(
                    line.line_number,
                    _VERDICT_TO_OUTCOME[result.verdict],
                    result.rule,
                    result.carc,
                    result.message,
                    None,
                )
                break

        if decision is None:
            decision, acc = _price(line, ctx, acc)
            trace.append(
                RuleResult(
                    PRICING,
                    Verdict.PASS if decision.pricing else Verdict.PEND,
                    decision.message,
                    None,
                    line.line_number,
                )
            )
        decisions.append(decision)

    pended = any(d.outcome is LineOutcome.PENDED for d in decisions)
    return AdjudicationResult(
        ClaimOutcome.PENDED if pended else ClaimOutcome.ADJUDICATED,
        tuple(decisions),
        tuple(trace),
        # A pended claim changes nothing; its accumulators are applied when it's re-adjudicated.
        ctx.accumulators if pended else acc,
    )


def _price(
    line: LineInput, ctx: AdjudicationContext, acc: Accumulators
) -> tuple[LineDecision, Accumulators]:
    fee = ctx.fee_schedule.get(line.procedure_code)
    if fee is None:
        # Covered but no contracted rate: a human prices it ("manual pricing").
        return (
            LineDecision(
                line.line_number,
                LineOutcome.PENDED,
                PRICING,
                None,
                "No fee schedule rate; needs manual pricing",
                None,
            ),
            acc,
        )

    pricing, acc = price_line(
        billed=line.billed_amount,
        rate_per_unit=fee.rate_per_unit,
        units=line.units,
        copay=ctx.benefits[line.procedure_code].copay,
        terms=ctx.plan_terms,
        acc=acc,
    )
    return (
        LineDecision(
            line.line_number,
            LineOutcome.APPROVED,
            PRICING,
            None,
            f"Allowed {pricing.allowed}, paid {pricing.paid}, "
            f"patient {pricing.patient_responsibility}",
            pricing,
        ),
        acc,
    )
