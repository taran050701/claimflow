"""Load demo claims that show every path through the system.

Runs on every deploy/startup, so it must be idempotent: each scenario submits its
claim with a fixed Idempotency-Key, and a scenario whose claim already exists is
skipped. Claims go through the real service (validation, rules engine, state
machine, audit log), so the demo data is produced exactly like real data.

Dates are relative to today so claims never age past timely filing.

    uv run python -m scripts.demo_seed
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from claimflow.config import get_settings
from claimflow.db import SessionLocal
from claimflow.domain.adjudication import AuthStatus
from claimflow.logging_config import configure_logging
from claimflow.models import MemberAccumulator, PriorAuthorization
from claimflow.schemas import ClaimIn
from claimflow.services.claim_service import ClaimService
from scripts.seed import insert_if_missing, seed

TODAY = date.today()
FAMILY_MEDICINE, IMAGING, ORTHOPEDICS = "1234567893", "1987654328", "1543210987"


@dataclass
class Scenario:
    key: str  # Idempotency-Key, also how the scenario is recognised on re-runs
    story: str
    claim: dict
    steps: list[str] = field(default_factory=lambda: ["validate", "adjudicate", "finalize"])
    before: Callable[[Session], None] | None = None  # set up auths/accumulators first


def claim(member: str, npi: str, days_ago: int, dx: list[str], lines: list[tuple]) -> dict:
    """lines: (cpt, units, billed)"""
    return {
        "member_id": member,
        "provider_npi": npi,
        "date_of_service": (TODAY - timedelta(days=days_ago)).isoformat(),
        "diagnosis_codes": dx,
        "total_billed": str(sum(Decimal(b) for _, _, b in lines)),
        "lines": [{"procedure_code": c, "units": u, "billed_amount": b} for c, u, b in lines],
    }


def auth(number: str, member: str, cpt: str, status: AuthStatus, days_ago: int):
    """An auth valid around the date of service."""

    def create(session: Session) -> None:
        dos = TODAY - timedelta(days=days_ago)
        insert_if_missing(
            session,
            PriorAuthorization,
            [
                PriorAuthorization(
                    auth_number=number,
                    member_id=member,
                    procedure_code=cpt,
                    status=status,
                    valid_from=dos - timedelta(days=30),
                    valid_to=dos + timedelta(days=30),
                )
            ],
        )
        session.commit()

    return create


def mri_lesson_setup(session: Session) -> None:
    """The lessons' MRI example: $250 of a $500 deductible already met."""
    year = (TODAY - timedelta(days=25)).year
    insert_if_missing(
        session,
        MemberAccumulator,
        [
            MemberAccumulator(
                member_id="M1001",
                plan_year=year,
                deductible_met=Decimal("250.00"),
                out_of_pocket_met=Decimal("250.00"),
            )
        ],
    )
    auth("PA-DEMO-MRI", "M1001", "72148", AuthStatus.APPROVED, 25)(session)


SCENARIOS = [
    Scenario(
        "demo-mri-lesson",
        "PAID: the lessons' MRI example. Billed $1,500, allowed $600, paid $280, patient $320",
        claim("M1001", IMAGING, 25, ["M54.50"], [("72148", 1, "1500.00")]),
        before=mri_lesson_setup,
    ),
    Scenario(
        "demo-office-visit",
        "PAID: office visit ($25 copay) + strep test (coinsurance)",
        claim(
            "M1001", FAMILY_MEDICINE, 20, ["J02.0"], [("99213", 1, "150.00"), ("87880", 1, "40.00")]
        ),
    ),
    Scenario(
        "demo-duplicate",
        "DENIED CO-18: the same office visit billed again",
        claim(
            "M1001", FAMILY_MEDICINE, 20, ["J02.0"], [("99213", 1, "150.00"), ("87880", 1, "40.00")]
        ),
    ),
    Scenario(
        "demo-no-auth",
        "DENIED CO-197: knee MRI without the required prior authorization",
        claim("M1004", IMAGING, 18, ["M17.11"], [("73721", 1, "1400.00")]),
    ),
    Scenario(
        "demo-not-covered",
        "DENIED CO-96: physical therapy is not covered on the Bronze plan",
        claim("M1002", ORTHOPEDICS, 15, ["M17.11"], [("97110", 2, "160.00")]),
    ),
    Scenario(
        "demo-terminated",
        "DENIED CO-27: member's coverage ended 2026-06-30",
        claim("M1003", FAMILY_MEDICINE, 12, ["I10"], [("99214", 1, "200.00")]),
    ),
    Scenario(
        "demo-too-many-units",
        "DENIED CO-151: 3 office visits billed for one day (limit 1)",
        claim("M1002", FAMILY_MEDICINE, 10, ["E11.9"], [("99214", 3, "600.00")]),
    ),
    Scenario(
        "demo-partial",
        "PAID with one line denied: visit approved ($50 copay), therapy not covered",
        claim("M1002", ORTHOPEDICS, 8, ["M17.11"], [("99213", 1, "150.00"), ("97110", 1, "80.00")]),
    ),
    Scenario(
        "demo-pended",
        "PENDED: knee MRI waiting on an authorization still under review",
        claim("M1002", IMAGING, 6, ["M17.11"], [("73721", 1, "1400.00")]),
        steps=["validate", "adjudicate"],
        before=auth("PA-DEMO-PEND", "M1002", "73721", AuthStatus.PENDING, 6),
    ),
    Scenario(
        "demo-rejected",
        "REJECTED: unknown member and a made-up procedure code",
        claim("M9999", FAMILY_MEDICINE, 5, ["J06.9"], [("00000", 1, "100.00")]),
        steps=["validate"],
    ),
    Scenario(
        "demo-appeal-overturned",
        "PAID after appeal: surgery denied (auth denied), retro auth approved, re-adjudicated",
        claim("M1004", ORTHOPEDICS, 30, ["S83.241A"], [("29881", 1, "4500.00")]),
        steps=[
            "validate",
            "adjudicate",
            "finalize",
            "appeal",
            "retro_auth",
            "adjudicate",
            "finalize",
        ],
    ),
]


def run_scenario(session: Session, scenario: Scenario) -> str:
    service = ClaimService(session)
    if scenario.before:
        scenario.before(session)
    claim_record, created = service.submit(ClaimIn.model_validate(scenario.claim), scenario.key)
    if not created:
        return f"exists   {claim_record.status:<9} {scenario.key}"

    for step in scenario.steps:
        if step == "appeal":
            service.appeal(claim_record.id, "Retroactive authorization requested")
        elif step == "retro_auth":
            auth("PA-DEMO-RETRO", "M1004", "29881", AuthStatus.APPROVED, 30)(session)
        else:
            getattr(service, step)(claim_record.id)
    return f"created  {claim_record.status:<9} {scenario.key}  {scenario.story}"


def load_demo() -> None:
    seed()
    with SessionLocal() as session:
        for scenario in SCENARIOS:
            print(run_scenario(session, scenario))


if __name__ == "__main__":
    # Runs outside the web app, so configure logging here too: production log
    # lines must all be JSON, or the platform's log search can't parse them.
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    load_demo()
