"""Aggregate queries for business KPIs. Computed from the database, so they
survive restarts (unlike in-memory Prometheus counters)."""

from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from claimflow.domain.enums import AuditEventType
from claimflow.models import AuditEvent, Claim, ClaimLineAdjudication


class StatsRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def claims_by_status(self) -> dict[str, int]:
        rows = self.session.execute(select(Claim.status, func.count()).group_by(Claim.status))
        return {status: count for status, count in rows}

    def line_outcomes(self) -> dict[str, int]:
        rows = self.session.execute(
            select(ClaimLineAdjudication.outcome, func.count()).group_by(
                ClaimLineAdjudication.outcome
            )
        )
        return {outcome: count for outcome, count in rows}

    def denial_reasons(self, limit: int = 10) -> list[tuple[str, str, int]]:
        """(carc, decided_by rule, count) for denied lines, most common first."""
        count = func.count().label("n")
        rows = self.session.execute(
            select(ClaimLineAdjudication.carc, ClaimLineAdjudication.decided_by, count)
            .where(ClaimLineAdjudication.outcome == "DENIED")
            .group_by(ClaimLineAdjudication.carc, ClaimLineAdjudication.decided_by)
            .order_by(count.desc(), ClaimLineAdjudication.carc)
            .limit(limit)
        )
        return [(carc, rule, n) for carc, rule, n in rows]

    def adjudication_latency_ms(self) -> tuple[int, float | None, float | None]:
        """(runs, p50, p95) from the duration recorded in each adjudication's audit event."""
        duration = cast(AuditEvent.details["duration_ms"].astext, Float)
        row = self.session.execute(
            select(
                func.count(duration),
                func.percentile_cont(0.5).within_group(duration),
                func.percentile_cont(0.95).within_group(duration),
            ).where(
                AuditEvent.event_type.in_(
                    [AuditEventType.CLAIM_ADJUDICATED, AuditEventType.CLAIM_PENDED]
                )
            )
        ).one()
        return row[0], row[1], row[2]
