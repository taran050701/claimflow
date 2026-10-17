"""Load reference data (plans, members, coverages, providers).

Safe to run repeatedly: `uv run python scripts/seed.py`.
Fuller seed data, including demo claims, arrives in Phase 8.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import delete

from claimflow.db import SessionLocal
from claimflow.domain.adjudication import AuthStatus
from claimflow.models import (
    CptCode,
    FeeScheduleEntry,
    Icd10Code,
    Member,
    MemberAccumulator,
    MemberCoverage,
    Plan,
    PlanBenefit,
    PriorAuthorization,
    Provider,
)

PLANS = [
    Plan(
        id="PLN-GOLD",
        name="Gold PPO",
        annual_deductible=Decimal("500.00"),
        coinsurance_rate=Decimal("0.200"),
        out_of_pocket_max=Decimal("4000.00"),
    ),
    Plan(
        id="PLN-BRONZE",
        name="Bronze HMO",
        annual_deductible=Decimal("3000.00"),
        coinsurance_rate=Decimal("0.400"),
        out_of_pocket_max=Decimal("8000.00"),
    ),
]

MEMBERS = [
    Member(id="M1001", first_name="Ava", last_name="Patel", date_of_birth=date(1988, 3, 14)),
    Member(id="M1002", first_name="Liam", last_name="Chen", date_of_birth=date(1975, 11, 2)),
    Member(id="M1003", first_name="Noah", last_name="Garcia", date_of_birth=date(1992, 7, 21)),
    Member(id="M1004", first_name="Mia", last_name="Johnson", date_of_birth=date(2001, 1, 30)),
]

# (member, plan, start, end). end=None means still active.
COVERAGES = [
    ("M1001", "PLN-GOLD", date(2026, 1, 1), None),  # active all year
    ("M1002", "PLN-BRONZE", date(2026, 1, 1), None),  # active, high deductible
    ("M1003", "PLN-GOLD", date(2026, 1, 1), date(2026, 6, 30)),  # terminated mid-year
    ("M1004", "PLN-BRONZE", date(2026, 1, 1), date(2026, 6, 30)),  # switched plans:
    ("M1004", "PLN-GOLD", date(2026, 7, 1), None),  # bronze -> gold on Jul 1
]

# NPIs are 10 digits with a Luhn check digit (validated in Phase 2).
PROVIDERS = [
    Provider(npi="1234567893", name="Riverside Family Medicine", specialty="Family Medicine"),
    Provider(npi="1987654328", name="Lakeview Imaging Center", specialty="Radiology"),
    Provider(npi="1543210987", name="Summit Orthopedics", specialty="Orthopedic Surgery"),
]

# A small hand-picked code set. CPT is AMA-copyrighted, so these descriptions are
# our own short paraphrases, not the official descriptors.
# (code, description, effective_start, effective_end)
CPT_CODES = [
    ("99203", "New patient office visit, low complexity", date(2000, 1, 1), None),
    ("99212", "Established patient office visit, straightforward", date(2000, 1, 1), None),
    ("99213", "Established patient office visit, low complexity", date(2000, 1, 1), None),
    ("99214", "Established patient office visit, moderate complexity", date(2000, 1, 1), None),
    ("99215", "Established patient office visit, high complexity", date(2000, 1, 1), None),
    (
        "99201",
        "New patient office visit, minimal (deleted 2021)",
        date(2000, 1, 1),
        date(2020, 12, 31),
    ),
    ("87880", "Rapid strep test", date(2000, 1, 1), None),
    ("80053", "Comprehensive metabolic blood panel", date(2000, 1, 1), None),
    ("85025", "Complete blood count with differential", date(2000, 1, 1), None),
    ("36415", "Routine blood draw", date(2000, 1, 1), None),
    ("93000", "Electrocardiogram with interpretation", date(2000, 1, 1), None),
    ("71046", "Chest X-ray, two views", date(2018, 1, 1), None),
    ("72148", "MRI lower spine without contrast", date(2000, 1, 1), None),
    ("73721", "MRI knee joint without contrast", date(2000, 1, 1), None),
    ("29881", "Knee arthroscopy with meniscectomy", date(2000, 1, 1), None),
    ("20610", "Large joint injection or aspiration", date(2000, 1, 1), None),
    ("97110", "Therapeutic exercise, each 15 minutes", date(2000, 1, 1), None),
    ("90686", "Flu vaccine, preservative free", date(2000, 1, 1), None),
    ("90471", "Vaccine administration, first", date(2000, 1, 1), None),
]

# ICD-10-CM went live in the US on 2015-10-01. M54.5 shows a real retirement:
# it was split into M54.50/M54.51/M54.59 on 2021-10-01.
ICD10_CODES = [
    ("J06.9", "Acute upper respiratory infection, unspecified", date(2015, 10, 1), None),
    ("J02.0", "Streptococcal pharyngitis", date(2015, 10, 1), None),
    ("M54.5", "Low back pain (retired 2021)", date(2015, 10, 1), date(2021, 9, 30)),
    ("M54.50", "Low back pain, unspecified", date(2021, 10, 1), None),
    ("M17.11", "Primary osteoarthritis, right knee", date(2015, 10, 1), None),
    ("S83.241A", "Medial meniscus tear, right knee, initial encounter", date(2015, 10, 1), None),
    ("E11.9", "Type 2 diabetes without complications", date(2015, 10, 1), None),
    ("E78.5", "Hyperlipidemia, unspecified", date(2015, 10, 1), None),
    ("I10", "Essential (primary) hypertension", date(2015, 10, 1), None),
    ("R07.9", "Chest pain, unspecified", date(2015, 10, 1), None),
    ("R51.9", "Headache, unspecified", date(2020, 10, 1), None),
    ("Z00.00", "Adult general exam without abnormal findings", date(2015, 10, 1), None),
    ("Z23", "Encounter for immunization", date(2015, 10, 1), None),
]


# Contracted rate per unit, and max units per day (MUE). 72148 = $600 matches the
# MRI example in the lessons.
FEE_SCHEDULE = {
    "99203": ("110.00", 1),
    "99212": ("60.00", 1),
    "99213": ("90.00", 1),
    "99214": ("130.00", 1),
    "99215": ("180.00", 1),
    "87880": ("20.00", 2),
    "80053": ("15.00", 1),
    "85025": ("10.00", 1),
    "36415": ("5.00", 1),
    "93000": ("25.00", 1),
    "71046": ("45.00", 1),
    "72148": ("600.00", 1),
    "73721": ("550.00", 1),
    "29881": ("1200.00", 1),
    "20610": ("75.00", 2),
    "97110": ("35.00", 4),
    "90686": ("20.00", 1),
    "90471": ("25.00", 1),
}

OFFICE_VISITS = ["99203", "99212", "99213", "99214", "99215"]
PREVENTIVE = ["90686", "90471"]  # vaccines: $0 copay
DEDUCTIBLE_SERVICES = ["87880", "80053", "85025", "36415", "93000", "71046", "20610"]
NEEDS_AUTH = ["72148", "73721", "29881"]  # MRIs and surgery


def _benefits(plan_id: str, visit_copay: str, covers_therapy: bool) -> list[PlanBenefit]:
    rows = [
        PlanBenefit(
            plan_id=plan_id,
            procedure_code=c,
            covered=True,
            requires_prior_auth=False,
            copay=Decimal(visit_copay),
        )
        for c in OFFICE_VISITS
    ]
    rows += [
        PlanBenefit(
            plan_id=plan_id,
            procedure_code=c,
            covered=True,
            requires_prior_auth=False,
            copay=Decimal("0.00"),
        )
        for c in PREVENTIVE
    ]
    rows += [
        PlanBenefit(
            plan_id=plan_id, procedure_code=c, covered=True, requires_prior_auth=False, copay=None
        )
        for c in DEDUCTIBLE_SERVICES
    ]
    rows += [
        PlanBenefit(
            plan_id=plan_id, procedure_code=c, covered=True, requires_prior_auth=True, copay=None
        )
        for c in NEEDS_AUTH
    ]
    # Physical therapy: covered on Gold, NOT on Bronze (demonstrates CO-96).
    rows.append(
        PlanBenefit(
            plan_id=plan_id,
            procedure_code="97110",
            covered=covers_therapy,
            requires_prior_auth=False,
            copay=None,
        )
    )
    return rows


PLAN_BENEFITS = _benefits("PLN-GOLD", "25.00", True) + _benefits("PLN-BRONZE", "50.00", False)

PRIOR_AUTHS = [
    PriorAuthorization(
        auth_number="PA-1001",
        member_id="M1001",
        procedure_code="72148",
        status=AuthStatus.APPROVED,
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 12, 31),
    ),
    PriorAuthorization(
        auth_number="PA-1002",
        member_id="M1002",
        procedure_code="73721",
        status=AuthStatus.PENDING,
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 12, 31),
    ),
    PriorAuthorization(
        auth_number="PA-1004",
        member_id="M1004",
        procedure_code="29881",
        status=AuthStatus.DENIED,
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 12, 31),
    ),
]

# M1001 has met $250 of their $500 Gold deductible -> the MRI example in the lessons.
ACCUMULATORS = [
    MemberAccumulator(
        member_id="M1001",
        plan_year=2026,
        deductible_met=Decimal("250.00"),
        out_of_pocket_met=Decimal("250.00"),
    ),
]


def seed() -> None:
    with SessionLocal() as session:
        # merge = insert, or update if a row with that primary key exists (an upsert).
        codes = [
            CptCode(code=c, description=d, effective_start=s, effective_end=e)
            for c, d, s, e in CPT_CODES
        ]
        codes += [
            Icd10Code(code=c, description=d, effective_start=s, effective_end=e)
            for c, d, s, e in ICD10_CODES
        ]
        for obj in [*PLANS, *MEMBERS, *PROVIDERS, *codes]:
            session.merge(obj)
        session.flush()  # parents first, so the rows below satisfy their foreign keys
        fees = [
            FeeScheduleEntry(procedure_code=c, rate_per_unit=Decimal(r), max_units_per_day=m)
            for c, (r, m) in FEE_SCHEDULE.items()
        ]
        for obj in [*fees, *PLAN_BENEFITS, *PRIOR_AUTHS, *ACCUMULATORS]:
            session.merge(obj)

        # Coverages have generated ids, so replace them wholesale instead of merging.
        member_ids = [m.id for m in MEMBERS]
        session.execute(delete(MemberCoverage).where(MemberCoverage.member_id.in_(member_ids)))
        session.add_all(
            MemberCoverage(member_id=m, plan_id=p, start_date=s, end_date=e)
            for m, p, s, e in COVERAGES
        )
        session.commit()
    print(
        f"Seeded {len(PLANS)} plans, {len(MEMBERS)} members, "
        f"{len(COVERAGES)} coverages, {len(PROVIDERS)} providers, "
        f"{len(CPT_CODES)} CPT codes, {len(ICD10_CODES)} ICD-10 codes, "
        f"{len(FEE_SCHEDULE)} fees, {len(PLAN_BENEFITS)} benefits, {len(PRIOR_AUTHS)} auths."
    )


if __name__ == "__main__":
    seed()
