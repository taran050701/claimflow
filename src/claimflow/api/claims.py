"""HTTP endpoints for claims. This layer only translates HTTP <-> service calls."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status
from sqlalchemy.orm import Session

from claimflow.db import get_session
from claimflow.domain.enums import ClaimStatus
from claimflow.schemas import AppealIn, ClaimIn, ClaimOut, ClaimPage, RemittanceOut
from claimflow.services.claim_service import ClaimService

router = APIRouter(prefix="/claims", tags=["claims"])


def get_claim_service(session: Annotated[Session, Depends(get_session)]) -> ClaimService:
    return ClaimService(session)


ServiceDep = Annotated[ClaimService, Depends(get_claim_service)]


@router.post(
    "",
    response_model=ClaimOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "Replay: a claim with this Idempotency-Key already exists"},
        409: {"description": "Idempotency-Key reused with a different request body"},
    },
)
def submit_claim(
    claim_in: ClaimIn,
    response: Response,
    service: ServiceDep,
    idempotency_key: Annotated[str | None, Header(max_length=100)] = None,
) -> ClaimOut:
    """Submit a claim. It is stored with status RECEIVED.

    Send an `Idempotency-Key` header to make retries safe: repeating the same
    request returns the original claim instead of creating a duplicate.
    """
    claim, created = service.submit(claim_in, idempotency_key)
    if not created:
        response.status_code = status.HTTP_200_OK
    return ClaimOut.model_validate(claim)


@router.get("/{claim_id}", response_model=ClaimOut)
def get_claim(claim_id: uuid.UUID, service: ServiceDep) -> ClaimOut:
    return ClaimOut.model_validate(service.get(claim_id))


@router.post(
    "/{claim_id}/validate",
    response_model=ClaimOut,
    responses={409: {"description": "Claim is not in RECEIVED status"}},
)
def validate_claim(claim_id: uuid.UUID, service: ServiceDep) -> ClaimOut:
    """Run front-end edits. The claim becomes VALIDATED, or REJECTED with every
    issue listed in `validation_issues` (each with a CARC reason code)."""
    return ClaimOut.model_validate(service.validate(claim_id))


@router.post(
    "/{claim_id}/adjudicate",
    response_model=ClaimOut,
    responses={409: {"description": "Claim is not VALIDATED, PENDED or APPEALED"}},
)
def adjudicate_claim(claim_id: uuid.UUID, service: ServiceDep) -> ClaimOut:
    """Run the rules engine: Eligibility, then per line Coverage -> CodeEdits ->
    PriorAuth -> Duplicate -> Pricing. The claim becomes ADJUDICATED, or PENDED if
    any line can't be decided yet. Each line gets its decision and amounts."""
    return ClaimOut.model_validate(service.adjudicate(claim_id))


@router.post(
    "/{claim_id}/finalize",
    response_model=ClaimOut,
    responses={409: {"description": "Claim is not ADJUDICATED"}},
)
def finalize_claim(claim_id: uuid.UUID, service: ServiceDep) -> ClaimOut:
    """Release payment: PAID if any line was approved (even at $0), else DENIED."""
    return ClaimOut.model_validate(service.finalize(claim_id))


@router.post(
    "/{claim_id}/appeal",
    response_model=ClaimOut,
    responses={409: {"description": "Only DENIED claims can be appealed"}},
)
def appeal_claim(claim_id: uuid.UUID, appeal: AppealIn, service: ServiceDep) -> ClaimOut:
    """Appeal a denial: DENIED -> APPEALED. Then call /adjudicate to re-run the
    rules with current data; the result overturns (PAID) or upholds (DENIED)."""
    return ClaimOut.model_validate(service.appeal(claim_id, appeal.reason))


@router.get(
    "/{claim_id}/remittance",
    response_model=RemittanceOut,
    responses={409: {"description": "Claim is not finalized (PAID or DENIED)"}},
)
def get_remittance(claim_id: uuid.UUID, service: ServiceDep) -> RemittanceOut:
    """Remittance advice (modeled on the X12 835): billed, allowed, paid and patient
    responsibility per line and in total, with a group code + CARC for every unpaid
    dollar. Always balances: billed - adjustments = paid."""
    remit = service.remittance(claim_id)
    return RemittanceOut.model_validate({**remit.__dict__, "claim_id": claim_id})


@router.get("", response_model=ClaimPage)
def list_claims(
    service: ServiceDep,
    status_filter: Annotated[ClaimStatus | None, Query(alias="status")] = None,
    member_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ClaimPage:
    claims = service.list(status=status_filter, member_id=member_id, limit=limit, offset=offset)
    return ClaimPage(items=[ClaimOut.model_validate(c) for c in claims], limit=limit, offset=offset)
