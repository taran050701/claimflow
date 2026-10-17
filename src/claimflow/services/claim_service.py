"""Claim use cases. Each public method is one business operation and owns
one database transaction: everything it writes commits together or not at all.
"""

import hashlib
import json
import uuid
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from claimflow.domain.adjudication import (
    AdjudicationContext,
    AdjudicationResult,
    ClaimOutcome,
    LineOutcome,
    find_coverage,
)
from claimflow.domain.adjudication import adjudicate as run_rules_engine
from claimflow.domain.enums import AuditEventType, ClaimStatus
from claimflow.domain.errors import ClaimNotFoundError, IdempotencyKeyConflictError
from claimflow.domain.pricing import Accumulators
from claimflow.domain.state_machine import assert_action_allowed, assert_transition
from claimflow.domain.validation import ClaimInput, LineInput, ReferenceData, validate_claim
from claimflow.models import (
    AuditEvent,
    Claim,
    ClaimLine,
    ClaimLineAdjudication,
    ClaimValidationIssue,
    MemberAccumulator,
)
from claimflow.repositories.adjudication import AdjudicationRepository
from claimflow.repositories.claims import AuditRepository, ClaimRepository
from claimflow.repositories.reference import ReferenceRepository
from claimflow.schemas import ClaimIn


def _request_hash(claim_in: ClaimIn) -> str:
    """Fingerprint of the request body, used to detect a reused Idempotency-Key.

    sort_keys + fixed separators make the JSON canonical, so the same content
    always produces the same hash.
    """
    canonical = json.dumps(claim_in.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class ClaimService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.claims = ClaimRepository(session)
        self.audit = AuditRepository(session)
        self.reference = ReferenceRepository(session)
        self.adjudication = AdjudicationRepository(session)

    def submit(self, claim_in: ClaimIn, idempotency_key: str | None = None) -> tuple[Claim, bool]:
        """Store a new claim in RECEIVED status.

        Returns (claim, created). created is False when this was a retry of an
        earlier request with the same Idempotency-Key.
        """
        request_hash = _request_hash(claim_in)

        if idempotency_key is not None:
            existing = self.claims.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return self._replay(existing, request_hash), False

        claim = Claim(
            member_id=claim_in.member_id,
            provider_npi=claim_in.provider_npi,
            date_of_service=claim_in.date_of_service,
            diagnosis_codes=claim_in.diagnosis_codes,
            total_billed=claim_in.total_billed,
            status=ClaimStatus.RECEIVED,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            lines=[
                ClaimLine(
                    line_number=i,
                    procedure_code=line.procedure_code,
                    units=line.units,
                    billed_amount=line.billed_amount,
                )
                for i, line in enumerate(claim_in.lines, start=1)
            ],
        )

        try:
            self.claims.add(claim)
            self.session.flush()  # sends the INSERTs, so claim.id is set for the audit row
            self.audit.add(
                AuditEvent(
                    claim_id=claim.id,
                    event_type=AuditEventType.CLAIM_RECEIVED,
                    from_status=None,
                    to_status=ClaimStatus.RECEIVED,
                    details={
                        "line_count": len(claim.lines),
                        "total_billed": str(claim.total_billed),
                    },
                )
            )
            self.session.commit()
        except IntegrityError:
            # Race: two requests with the same key both passed the lookup above.
            # The unique constraint let only one insert succeed; this is the loser.
            self.session.rollback()
            if idempotency_key is None:
                raise
            existing = self.claims.get_by_idempotency_key(idempotency_key)
            if existing is None:
                raise
            return self._replay(existing, request_hash), False

        return claim, True

    def validate(self, claim_id: uuid.UUID) -> Claim:
        """Run the front-end edits. RECEIVED -> VALIDATED, or -> REJECTED with issues."""
        claim = self._load_for_action(claim_id, "validate")

        claim_input = _to_claim_input(claim)
        issues = validate_claim(claim_input, self._reference_data_for(claim_input))

        claim.validation_issues.extend(
            ClaimValidationIssue(
                line_number=i.line_number,
                edit_code=i.edit_code,
                carc=i.carc,
                field=i.field,
                message=i.message,
            )
            for i in issues
        )
        self._transition(
            claim,
            ClaimStatus.REJECTED if issues else ClaimStatus.VALIDATED,
            AuditEventType.CLAIM_REJECTED if issues else AuditEventType.CLAIM_VALIDATED,
            {
                "issues": [
                    {"edit_code": i.edit_code, "carc": i.carc, "line_number": i.line_number}
                    for i in issues
                ]
            },
        )
        self.session.commit()
        return claim

    def adjudicate(self, claim_id: uuid.UUID) -> Claim:
        """Run the rules engine -> ADJUDICATED or PENDED.

        Allowed from VALIDATED, PENDED (retry once the pend is resolved) and
        APPEALED (re-adjudication decides whether the denial is overturned).
        """
        claim = self._load_for_action(claim_id, "adjudicate")

        claim_input = _to_claim_input(claim)
        # Lock order: claim row, then the member's accumulator row. Always the same
        # order, so two transactions can never wait on each other in a cycle (deadlock).
        accumulator = self.adjudication.lock_accumulator(
            claim.member_id, claim.date_of_service.year
        )
        ctx = self._adjudication_context(claim, claim_input, accumulator)
        result = run_rules_engine(ctx)

        self._replace_line_decisions(claim, result)
        if result.outcome is ClaimOutcome.ADJUDICATED:
            accumulator.deductible_met = result.accumulators_after.deductible_met
            accumulator.out_of_pocket_met = result.accumulators_after.out_of_pocket_met

        self._transition(
            claim,
            ClaimStatus(result.outcome),
            AuditEventType.CLAIM_ADJUDICATED
            if result.outcome is ClaimOutcome.ADJUDICATED
            else AuditEventType.CLAIM_PENDED,
            {
                "trace": [
                    {
                        "rule": r.rule,
                        "line_number": r.line_number,
                        "verdict": r.verdict,
                        "carc": r.carc,
                        "message": r.message,
                    }
                    for r in result.trace
                ],
                "accumulators_before": _accumulators_json(ctx.accumulators),
                "accumulators_after": _accumulators_json(result.accumulators_after),
            },
        )
        self.session.commit()
        return claim

    def finalize(self, claim_id: uuid.UUID) -> Claim:
        """Payment release: ADJUDICATED -> PAID if any line was approved (even at $0,
        e.g. all applied to the deductible), otherwise DENIED."""
        claim = self._load_for_action(claim_id, "finalize")
        approved = [
            line.adjudication
            for line in claim.lines
            if line.adjudication and line.adjudication.outcome is LineOutcome.APPROVED
        ]
        total_paid = sum((a.paid for a in approved), Decimal("0.00"))
        self._transition(
            claim,
            ClaimStatus.PAID if approved else ClaimStatus.DENIED,
            AuditEventType.CLAIM_PAID if approved else AuditEventType.CLAIM_DENIED,
            {"approved_lines": len(approved), "total_paid": str(total_paid)},
        )
        self.session.commit()
        return claim

    def appeal(self, claim_id: uuid.UUID, reason: str) -> Claim:
        """DENIED -> APPEALED. Re-adjudication then decides the outcome."""
        claim = self._load_for_action(claim_id, "appeal")
        self._transition(
            claim, ClaimStatus.APPEALED, AuditEventType.CLAIM_APPEALED, {"reason": reason}
        )
        self.session.commit()
        return claim

    # ------------------------------------------------------------ state helpers

    def _load_for_action(self, claim_id: uuid.UUID, action: str) -> Claim:
        """Lock the claim row and check the action is possible from its status,
        before doing any work."""
        claim = self.claims.get(claim_id, for_update=True)
        if claim is None:
            raise ClaimNotFoundError(claim_id)
        assert_action_allowed(claim.status, action)
        return claim

    def _transition(
        self, claim: Claim, target: ClaimStatus, event_type: AuditEventType, details: dict
    ) -> None:
        """The ONLY place a claim's status changes. Checks the transition table,
        changes the status, and records the audit event, every time."""
        assert_transition(claim.status, target)
        self.audit.add(
            AuditEvent(
                claim_id=claim.id,
                event_type=event_type,
                from_status=claim.status,
                to_status=target,
                details=details,
            )
        )
        claim.status = target

    def _adjudication_context(
        self, claim: Claim, claim_input: ClaimInput, accumulator: MemberAccumulator
    ) -> AdjudicationContext:
        codes = {line.procedure_code for line in claim_input.lines}
        spans = self.adjudication.coverage_spans(claim.member_id)
        # Same pure function the EligibilityRule uses, so "which plan?" has one answer.
        span = find_coverage(spans, claim.date_of_service)
        return AdjudicationContext(
            claim=claim_input,
            coverage_spans=spans,
            plan_terms=self.adjudication.plan_terms(span.plan_id) if span else None,
            benefits=self.adjudication.benefits(span.plan_id, codes) if span else {},
            fee_schedule=self.adjudication.fee_schedule(codes),
            prior_auths=self.adjudication.prior_auths(claim.member_id, codes),
            already_processed=self.adjudication.already_processed(
                claim.member_id, claim.date_of_service, claim.id
            ),
            accumulators=Accumulators(accumulator.deductible_met, accumulator.out_of_pocket_met),
        )

    def _replace_line_decisions(self, claim: Claim, result: AdjudicationResult) -> None:
        """Store one decision per line. A re-adjudicated PENDED claim replaces its
        earlier decisions: delete and flush first, because old and new rows share
        the same primary key (claim_line_id)."""
        if any(line.adjudication is not None for line in claim.lines):
            for line in claim.lines:
                line.adjudication = None  # delete-orphan cascade removes the old row
            self.session.flush()

        by_number = {line.line_number: line for line in claim.lines}
        for decision in result.lines:
            p = decision.pricing
            by_number[decision.line_number].adjudication = ClaimLineAdjudication(
                outcome=decision.outcome,
                decided_by=decision.decided_by,
                carc=decision.carc,
                message=decision.message,
                **(
                    {
                        "allowed": p.allowed,
                        "contractual_adjustment": p.contractual_adjustment,
                        "deductible": p.deductible,
                        "copay": p.copay,
                        "coinsurance": p.coinsurance,
                        "patient_responsibility": p.patient_responsibility,
                        "paid": p.paid,
                    }
                    if p
                    else {}
                ),
            )

    def _reference_data_for(self, claim: ClaimInput) -> ReferenceData:
        """Gather every database fact the (pure) validator needs, up front."""
        dos = claim.date_of_service
        return ReferenceData(
            member_date_of_birth=self.reference.member_date_of_birth(claim.member_id),
            provider_exists=self.reference.provider_exists(claim.provider_npi),
            valid_cpt_codes=self.reference.valid_cpt_codes(
                {line.procedure_code for line in claim.lines}, dos
            ),
            valid_icd_codes=self.reference.valid_icd_codes(set(claim.diagnosis_codes), dos),
        )

    def get(self, claim_id: uuid.UUID) -> Claim:
        claim = self.claims.get(claim_id)
        if claim is None:
            raise ClaimNotFoundError(claim_id)
        return claim

    def list(self, **filters) -> list[Claim]:
        return self.claims.list(**filters)

    @staticmethod
    def _replay(existing: Claim, request_hash: str) -> Claim:
        if existing.request_hash != request_hash:
            raise IdempotencyKeyConflictError(existing.idempotency_key)
        return existing


def _to_claim_input(claim: Claim) -> ClaimInput:
    """ORM object -> plain domain dataclass, so the domain never sees SQLAlchemy."""
    return ClaimInput(
        member_id=claim.member_id,
        provider_npi=claim.provider_npi,
        date_of_service=claim.date_of_service,
        received_date=claim.created_at.date(),
        diagnosis_codes=tuple(claim.diagnosis_codes),
        total_billed=claim.total_billed,
        lines=tuple(
            LineInput(
                line_number=line.line_number,
                procedure_code=line.procedure_code,
                units=line.units,
                billed_amount=line.billed_amount,
            )
            for line in claim.lines
        ),
    )


def _accumulators_json(acc: Accumulators) -> dict[str, str]:
    return {
        "deductible_met": str(acc.deductible_met),
        "out_of_pocket_met": str(acc.out_of_pocket_met),
    }
