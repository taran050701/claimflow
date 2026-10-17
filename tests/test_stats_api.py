"""GET /stats: business KPIs computed from the database."""

from datetime import date, timedelta

DOS = date.today() - timedelta(days=30)


def process(client, payload, *actions):
    claim_id = client.post("/claims", json=payload).json()["id"]
    for action in actions:
        assert client.post(f"/claims/{claim_id}/{action}").status_code == 200


def test_stats_on_empty_database(client):
    body = client.get("/stats").json()
    assert body["claims_by_status"] == {}
    assert body["claim_denial_rate"] is None
    assert body["line_denial_rate"] is None
    assert body["top_denial_reasons"] == []
    assert body["adjudication_latency"] == {"runs": 0, "p50_ms": None, "p95_ms": None}


def test_stats_after_a_mix_of_outcomes(client, claim_payload):
    full = ("validate", "adjudicate", "finalize")
    process(client, claim_payload, *full)  # PAID, 2 approved lines
    mri = {
        "member_id": "M1001",
        "provider_npi": "1987654328",
        "date_of_service": DOS.isoformat(),
        "diagnosis_codes": ["M54.50"],
        "total_billed": "1500.00",
        "lines": [{"procedure_code": "72148", "units": 1, "billed_amount": "1500.00"}],
    }
    process(client, mri, *full)  # DENIED, no auth (CO-197)
    process(client, {**claim_payload, "member_id": "M9999"}, "validate")  # REJECTED

    body = client.get("/stats").json()

    assert body["claims_by_status"] == {"PAID": 1, "DENIED": 1, "REJECTED": 1}
    assert body["claim_denial_rate"] == 0.5
    assert body["line_denial_rate"] == 0.3333  # 1 denied of 3 decided lines
    assert body["top_denial_reasons"] == [
        {
            "carc": "197",
            "reason": "Precertification/authorization absent",
            "rule": "PriorAuth",
            "lines": 1,
        }
    ]
    latency = body["adjudication_latency"]
    assert latency["runs"] == 2
    assert 0 < latency["p50_ms"] <= latency["p95_ms"]
