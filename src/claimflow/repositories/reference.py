"""Lookups against reference data: members, providers, code sets."""

from datetime import date

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from claimflow.models import CptCode, Icd10Code, Member, Provider


class ReferenceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def member_date_of_birth(self, member_id: str) -> date | None:
        return self.session.scalar(select(Member.date_of_birth).where(Member.id == member_id))

    def provider_exists(self, npi: str) -> bool:
        return bool(self.session.scalar(select(exists().where(Provider.npi == npi))))

    def valid_cpt_codes(self, codes: set[str], on: date) -> frozenset[str]:
        return self._valid_codes(CptCode, codes, on)

    def valid_icd_codes(self, codes: set[str], on: date) -> frozenset[str]:
        return self._valid_codes(Icd10Code, codes, on)

    def _valid_codes(self, model: type[CptCode | Icd10Code], codes: set[str], on: date):
        """Of the given codes, return those that were in effect on the given date.
        One query for the whole set, not one per code."""
        if not codes:
            return frozenset()
        stmt = select(model.code).where(
            model.code.in_(codes),
            model.effective_start <= on,
            or_(model.effective_end.is_(None), model.effective_end >= on),
        )
        return frozenset(self.session.scalars(stmt))
