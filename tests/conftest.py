"""Test setup.

Integration tests run against a real Postgres database (claimflow_test), not
SQLite or mocks: our schema uses Postgres features (ARRAY, JSONB, CHECK
constraints), and a test database that behaves differently from production
would give false confidence.
"""

import os

# Point the app at the test database BEFORE importing anything from claimflow,
# because claimflow.db creates its engine at import time.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://localhost:5432/claimflow_test"
)

from datetime import date, timedelta  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from claimflow.db import engine  # noqa: E402
from claimflow.main import app  # noqa: E402
from scripts.seed import seed  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    """Build the schema once per test run by running the real migrations.
    This also tests that the migrations work."""
    command.upgrade(Config("alembic.ini"), "head")
    seed()  # reference data: plans, members, providers, code sets
    yield


@pytest.fixture(autouse=True)
def clean_tables():
    """After each test: empty the claim tables and reset the mutable reference
    data (accumulators, auths), so tests can't affect each other."""
    yield
    reset_test_data()


def reset_test_data() -> None:
    # audit_events is append-only: its triggers refuse TRUNCATE. The table OWNER can
    # disable triggers, which is fine for wiping a test database (and is exactly why
    # production also needs least-privilege roles and the hash chain).
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
        conn.execute(
            text(
                "TRUNCATE audit_events, claim_line_adjudications, claim_validation_issues, "
                "claim_lines, claims, member_accumulators, prior_authorizations "
                "RESTART IDENTITY CASCADE"
            )
        )
        conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER USER"))


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def claim_payload() -> dict:
    """A claim that passes every edit: active member, known provider, valid codes.

    The date of service is relative to today so the claim never ages past the
    timely-filing limit as the calendar moves on.
    """
    return {
        "member_id": "M1001",
        "provider_npi": "1234567893",
        "date_of_service": (date.today() - timedelta(days=30)).isoformat(),
        "diagnosis_codes": ["J06.9"],
        "total_billed": "250.00",
        "lines": [
            {"procedure_code": "99213", "units": 1, "billed_amount": "150.00"},
            {"procedure_code": "87880", "units": 1, "billed_amount": "100.00"},
        ],
    }
