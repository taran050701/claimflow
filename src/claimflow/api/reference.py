"""Reference data: GET /reference, the choices a claim form offers."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from claimflow.db import get_session
from claimflow.repositories.reference import ReferenceRepository

router = APIRouter(tags=["ops"])


class CoverageOut(BaseModel):
    plan_id: str
    plan_name: str
    start_date: date
    end_date: date | None


class MemberOut(BaseModel):
    id: str
    name: str
    coverages: list[CoverageOut]


class ProviderOut(BaseModel):
    npi: str
    name: str
    specialty: str


class CodeOut(BaseModel):
    code: str
    description: str
    effective_start: date
    effective_end: date | None


class ReferenceOut(BaseModel):
    members: list[MemberOut]
    providers: list[ProviderOut]
    procedure_codes: list[CodeOut]
    diagnosis_codes: list[CodeOut]


def _code(row: object) -> CodeOut:
    return CodeOut.model_validate(row, from_attributes=True)


@router.get("/reference", response_model=ReferenceOut)
def reference(session: Annotated[Session, Depends(get_session)]) -> ReferenceOut:
    """Members (with plan coverage periods), providers, and the CPT and ICD-10 code
    sets with their effective dates: what a claim can reference. The dashboard's
    claim form is built from this, so it never drifts from the database."""
    repo = ReferenceRepository(session)
    return ReferenceOut(
        members=[
            MemberOut(
                id=m.id,
                name=f"{m.first_name} {m.last_name}",
                coverages=[
                    CoverageOut(
                        plan_id=c.plan_id,
                        plan_name=c.plan.name,
                        start_date=c.start_date,
                        end_date=c.end_date,
                    )
                    for c in sorted(m.coverages, key=lambda c: c.start_date)
                ],
            )
            for m in repo.members()
        ],
        providers=[ProviderOut.model_validate(p, from_attributes=True) for p in repo.providers()],
        procedure_codes=[_code(c) for c in repo.cpt_codes()],
        diagnosis_codes=[_code(c) for c in repo.icd_codes()],
    )
