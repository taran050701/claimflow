"""Business KPIs: GET /stats."""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from claimflow.db import get_session
from claimflow.domain.carc import describe
from claimflow.repositories.stats import StatsRepository

router = APIRouter(tags=["ops"])


class DenialReason(BaseModel):
    carc: str
    reason: str
    rule: str
    lines: int


class Latency(BaseModel):
    runs: int
    p50_ms: float | None
    p95_ms: float | None


class Dollars(BaseModel):
    """Where the billed money went, over finalized (PAID/DENIED) claims.
    Always balances: billed = paid + patient_responsibility + provider_write_off."""

    billed: Decimal
    allowed: Decimal
    paid: Decimal = Field(description="Paid by the insurer")
    patient_responsibility: Decimal = Field(description="Owed by patients (PR)")
    provider_write_off: Decimal = Field(description="Written off by providers (CO)")


class StatsOut(BaseModel):
    claims_by_status: dict[str, int]
    claim_denial_rate: float | None = Field(
        description="DENIED / (PAID + DENIED); null until a claim is finalized"
    )
    line_denial_rate: float | None = Field(description="Denied lines / decided lines")
    top_denial_reasons: list[DenialReason]
    adjudication_latency: Latency
    dollars: Dollars


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


@router.get("/stats", response_model=StatsOut)
def stats(session: Annotated[Session, Depends(get_session)]) -> StatsOut:
    """Claims processed, denial rates, top denial reasons, adjudication latency and
    where the money went,
    computed from the database (they survive restarts, unlike /metrics counters)."""
    repo = StatsRepository(session)
    by_status = repo.claims_by_status()
    lines = repo.line_outcomes()
    finalized = by_status.get("PAID", 0) + by_status.get("DENIED", 0)
    decided_lines = lines.get("APPROVED", 0) + lines.get("DENIED", 0)
    runs, p50, p95 = repo.adjudication_latency_ms()
    billed, allowed, paid, patient = repo.finalized_dollars()
    return StatsOut(
        claims_by_status=by_status,
        claim_denial_rate=_rate(by_status.get("DENIED", 0), finalized),
        line_denial_rate=_rate(lines.get("DENIED", 0), decided_lines),
        top_denial_reasons=[
            DenialReason(carc=carc, reason=describe(carc), rule=rule, lines=n)
            for carc, rule, n in repo.denial_reasons()
        ],
        adjudication_latency=Latency(
            runs=runs,
            p50_ms=round(p50, 2) if p50 is not None else None,
            p95_ms=round(p95, 2) if p95 is not None else None,
        ),
        dollars=Dollars(
            billed=billed,
            allowed=allowed,
            paid=paid,
            patient_responsibility=patient,
            provider_write_off=billed - paid - patient,
        ),
    )
