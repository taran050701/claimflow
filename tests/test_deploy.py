"""Deployment concerns: config from the host, the demo data, the landing page."""

import pytest
from sqlalchemy import func, select

from claimflow.config import Settings
from claimflow.db import SessionLocal
from claimflow.models import AuditEvent, Claim
from scripts.demo_seed import SCENARIOS, load_demo


@pytest.mark.parametrize(
    "given",
    [
        "postgres://u:p@host/db?sslmode=require",
        "postgresql://u:p@host/db?sslmode=require",
        "postgresql+psycopg://u:p@host/db?sslmode=require",
    ],
)
def test_database_url_from_any_host_gets_the_psycopg_driver(given):
    assert Settings(database_url=given).database_url == (
        "postgresql+psycopg://u:p@host/db?sslmode=require"
    )


EXPECTED = {
    "demo-mri-lesson": "PAID",
    "demo-office-visit": "PAID",
    "demo-duplicate": "DENIED",
    "demo-no-auth": "DENIED",
    "demo-not-covered": "DENIED",
    "demo-terminated": "DENIED",
    "demo-too-many-units": "DENIED",
    "demo-partial": "PAID",
    "demo-pended": "PENDED",
    "demo-rejected": "REJECTED",
    "demo-appeal-overturned": "PAID",
}


def demo_claims() -> dict[str, Claim]:
    with SessionLocal() as s:
        return {c.idempotency_key: c for c in s.scalars(select(Claim))}


def test_every_demo_scenario_reaches_its_intended_status(capsys):
    load_demo()

    claims = demo_claims()
    assert {key: c.status for key, c in claims.items()} == EXPECTED
    assert set(EXPECTED) == {s.key for s in SCENARIOS}


def test_demo_mri_reproduces_the_lesson_numbers(client, capsys):
    load_demo()
    mri = demo_claims()["demo-mri-lesson"]

    totals = client.get(f"/claims/{mri.id}/remittance").json()["totals"]

    assert (totals["allowed"], totals["paid"], totals["patient_responsibility"]) == (
        "600.00",
        "280.00",
        "320.00",
    )


def test_demo_denials_have_the_intended_reasons(client, capsys):
    load_demo()
    claims = demo_claims()
    expected = {
        "demo-duplicate": "18",
        "demo-no-auth": "197",
        "demo-not-covered": "96",
        "demo-terminated": "27",
        "demo-too-many-units": "151",
    }
    for key, carc in expected.items():
        lines = client.get(f"/claims/{claims[key].id}/explanation").json()["lines"]
        assert {li["reason_code"] for li in lines} == {carc}, key


def row_counts() -> tuple[int, int]:
    with SessionLocal() as s:
        return (
            s.scalar(select(func.count(Claim.id))),
            s.scalar(select(func.count(AuditEvent.id))),
        )


def test_demo_load_is_idempotent(capsys):
    load_demo()
    before = row_counts()
    capsys.readouterr()  # discard the first run's output

    load_demo()  # e.g. the free host woke up and ran the start script again

    assert row_counts() == before
    scenario_lines = [ln for ln in capsys.readouterr().out.splitlines() if "demo-" in ln]
    assert len(scenario_lines) == len(SCENARIOS)
    assert all(ln.startswith("exists") for ln in scenario_lines)


def test_root_redirects_to_swagger(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/docs"
    assert client.get("/docs").status_code == 200
