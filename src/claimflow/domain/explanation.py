"""Answer "why was this claim decided this way?" from audit records alone. Pure Python.

The explanation is derived only from the audit log, not from the current
claim tables, so it shows exactly what was recorded when the decision was made.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from claimflow.domain.carc import describe
from claimflow.domain.enums import AuditEventType as E

DECISION_EVENTS = {E.CLAIM_ADJUDICATED, E.CLAIM_PENDED}


@dataclass(frozen=True)
class AuditRecord:
    """The parts of an audit event an explanation needs."""

    seq: int
    event_type: str
    to_status: str | None
    rule_name: str | None
    line_number: int | None
    outcome: str | None
    reason_code: str | None
    correlation_id: uuid.UUID | None
    details: Mapping[str, Any]


@dataclass(frozen=True)
class LineExplanation:
    line_number: int
    procedure_code: str
    outcome: str  # APPROVED / DENIED / PENDED
    decided_by: str  # the decisive rule
    reason_code: str | None
    reason: str  # CARC description
    message: str
    inputs: Mapping[str, Any]  # what the decisive rule looked at
    rules_passed: tuple[str, ...]  # rules this line cleared before the decision


@dataclass(frozen=True)
class ValidationFailure:
    edit_code: str
    line_number: int | None
    reason_code: str | None
    reason: str
    message: str


@dataclass(frozen=True)
class Explanation:
    status: str
    summary: str
    lines: tuple[LineExplanation, ...]
    validation_failures: tuple[ValidationFailure, ...]


_VERDICT_TO_OUTCOME = {"PASS": "APPROVED", "DENY": "DENIED", "PEND": "PENDED"}


def explain(
    status: str, procedure_codes: Mapping[int, str], records: list[AuditRecord]
) -> Explanation:
    """records: every audit record for the claim, in seq order."""
    rejection = _last(records, {E.CLAIM_REJECTED})
    decision = _last(records, DECISION_EVENTS)

    if rejection is not None:
        failures = tuple(
            ValidationFailure(
                edit_code=r.rule_name or "",
                line_number=r.line_number,
                reason_code=r.reason_code,
                reason=describe(r.reason_code),
                message=r.details.get("message", ""),
            )
            for r in records
            if r.correlation_id == rejection.correlation_id
            and r.event_type == E.VALIDATION_EDIT_FAILED
        )
        edits = ", ".join(f.edit_code for f in failures)
        return Explanation(
            status, f"Rejected in validation ({len(failures)} issue(s): {edits})", (), failures
        )

    if decision is None:
        return Explanation(status, "No adjudication decision has been made yet", (), ())

    run = [
        r
        for r in records
        if r.correlation_id == decision.correlation_id and r.event_type == E.RULE_EVALUATED
    ]
    claim_level_denial = next(
        (r for r in run if r.line_number is None and r.outcome != "PASS"), None
    )
    lines = tuple(
        _explain_line(n, code, run, claim_level_denial)
        for n, code in sorted(procedure_codes.items())
    )
    return Explanation(status, _summary(status, lines), lines, ())


def _explain_line(
    number: int, code: str, run: list[AuditRecord], claim_level_denial: AuditRecord | None
) -> LineExplanation:
    if claim_level_denial is not None:
        decisive, passed = claim_level_denial, ()
    else:
        # Claim-level rules (Eligibility) ran first for every line, then the line's own.
        cleared = [r for r in run if r.line_number is None]
        evaluated = [r for r in run if r.line_number == number]
        decisive = evaluated[-1]  # short-circuit: the last rule evaluated decided the line
        passed = tuple(r.rule_name or "" for r in cleared + evaluated[:-1])
    return LineExplanation(
        line_number=number,
        procedure_code=code,
        outcome=_VERDICT_TO_OUTCOME[decisive.outcome or "PASS"],
        decided_by=decisive.rule_name or "",
        reason_code=decisive.reason_code,
        reason=describe(decisive.reason_code),
        message=decisive.details.get("message", ""),
        inputs=decisive.details.get("inputs", {}),
        rules_passed=passed,
    )


def _summary(status: str, lines: tuple[LineExplanation, ...]) -> str:
    counts = {
        o: sum(1 for li in lines if li.outcome == o) for o in ("APPROVED", "DENIED", "PENDED")
    }
    parts = [f"{n} {o.lower()}" for o, n in counts.items() if n]
    denials = [
        f"line {li.line_number} ({li.procedure_code}) denied by {li.decided_by}: "
        f"CO-{li.reason_code} {li.reason}"
        for li in lines
        if li.outcome == "DENIED"
    ]
    return f"{status}: " + ", ".join(parts) + ("; " + "; ".join(denials) if denials else "")


def _last(records: list[AuditRecord], types: set[E]) -> AuditRecord | None:
    return next((r for r in reversed(records) if r.event_type in types), None)
