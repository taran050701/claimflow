"""tamper evident audit log

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-17 00:37:00.771874

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Hash of one event, chained to the previous event's hash. Computed ONLY by the
# database (in the trigger below and in verify_audit_chain), so there is one
# implementation. Every field is rendered to deterministic text: jsonb::text has
# a canonical key order, and created_at is formatted in UTC so the session's
# time zone can't change the result.
AUDIT_EVENT_HASH = """
CREATE FUNCTION audit_event_hash(e audit_events) RETURNS text AS $$
    SELECT encode(sha256(convert_to(concat_ws('|',
        e.prev_hash, e.claim_id::text, e.seq::text, e.event_type,
        coalesce(e.from_status, ''), coalesce(e.to_status, ''),
        coalesce(e.rule_name, ''), coalesce(e.line_number::text, ''),
        coalesce(e.outcome, ''), coalesce(e.reason_code, ''),
        coalesce(e.correlation_id::text, ''), e.details::text,
        to_char(e.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US')
    ), 'UTF8')), 'hex')
$$ LANGUAGE sql STABLE;
"""

# Before each insert: next seq for the claim, link to the previous hash, stamp the
# time, then hash. Writes for one claim are serialized by the claim row lock
# (SELECT ... FOR UPDATE in the service), so "max seq + 1" can't race; the
# UNIQUE(claim_id, seq) constraint is the backstop if it ever did.
CHAIN_TRIGGER = """
CREATE FUNCTION audit_events_chain() RETURNS trigger AS $$
DECLARE
    prev audit_events;
BEGIN
    SELECT * INTO prev FROM audit_events
    WHERE claim_id = NEW.claim_id ORDER BY seq DESC LIMIT 1;
    NEW.seq := coalesce(prev.seq, 0) + 1;
    NEW.prev_hash := coalesce(prev.hash, repeat('0', 64));
    NEW.created_at := clock_timestamp();
    NEW.hash := audit_event_hash(NEW);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_events_chain BEFORE INSERT ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_chain();
"""

APPEND_ONLY = """
CREATE FUNCTION audit_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_events_no_update_delete BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();
CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only();
"""

# Returns NULL if the claim's chain is intact, otherwise the first bad seq:
# a gap (deleted event), a broken link, or contents that no longer match the hash.
VERIFY = """
CREATE FUNCTION verify_audit_chain(p_claim_id uuid) RETURNS integer AS $$
DECLARE
    e audit_events;
    expected_seq integer := 1;
    expected_prev text := repeat('0', 64);
BEGIN
    FOR e IN SELECT * FROM audit_events WHERE claim_id = p_claim_id ORDER BY seq LOOP
        IF e.seq <> expected_seq OR e.prev_hash <> expected_prev
           OR e.hash <> audit_event_hash(e) THEN
            RETURN expected_seq;
        END IF;
        expected_prev := e.hash;
        expected_seq := expected_seq + 1;
    END LOOP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;
"""

# Existing rows get seq/prev_hash/hash computed in claim order, exactly as the
# trigger would have done.
BACKFILL = """
DO $$
DECLARE
    e audit_events;
    current_claim uuid;
    n integer;
    last_hash text;
BEGIN
    FOR e IN SELECT * FROM audit_events ORDER BY claim_id, id LOOP
        IF current_claim IS DISTINCT FROM e.claim_id THEN
            current_claim := e.claim_id;
            n := 0;
            last_hash := repeat('0', 64);
        END IF;
        n := n + 1;
        e.seq := n;
        e.prev_hash := last_hash;
        e.hash := audit_event_hash(e);
        UPDATE audit_events SET seq = e.seq, prev_hash = e.prev_hash, hash = e.hash
        WHERE id = e.id;
        last_hash := e.hash;
    END LOOP;
END $$;
"""


def upgrade() -> None:
    # Reviewed by hand. Order matters:
    # 1. add columns NULLABLE (the table already has rows; NOT NULL would fail)
    for name, type_ in [
        ("seq", sa.Integer()),
        ("correlation_id", sa.Uuid()),
        ("rule_name", sa.String(length=40)),
        ("line_number", sa.Integer()),
        ("outcome", sa.String(length=10)),
        ("reason_code", sa.String(length=5)),
        ("prev_hash", sa.String(length=64)),
        ("hash", sa.String(length=64)),
    ]:
        op.add_column("audit_events", sa.Column(name, type_, nullable=True))

    # 2. hash function, then backfill existing rows (BEFORE the append-only trigger
    #    exists, or it would block these UPDATEs)
    op.execute(AUDIT_EVENT_HASH)
    op.execute(BACKFILL)

    # 3. now every row has values: tighten to NOT NULL
    for name in ("seq", "prev_hash", "hash"):
        op.alter_column("audit_events", name, nullable=False)

    op.drop_index(op.f("ix_audit_events_claim_created"), table_name="audit_events")
    op.create_index("ix_audit_events_rule_outcome", "audit_events", ["rule_name", "outcome"])
    op.create_unique_constraint("uq_audit_events_claim_seq", "audit_events", ["claim_id", "seq"])

    # 4. triggers and the verifier
    op.execute(CHAIN_TRIGGER)
    op.execute(VERIFY)
    op.execute(APPEND_ONLY)


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_events_no_truncate ON audit_events")
    op.execute("DROP TRIGGER audit_events_no_update_delete ON audit_events")
    op.execute("DROP FUNCTION audit_events_append_only()")
    op.execute("DROP FUNCTION verify_audit_chain(uuid)")
    op.execute("DROP TRIGGER audit_events_chain ON audit_events")
    op.execute("DROP FUNCTION audit_events_chain()")
    op.drop_constraint("uq_audit_events_claim_seq", "audit_events", type_="unique")
    op.drop_index("ix_audit_events_rule_outcome", table_name="audit_events")
    op.create_index(
        op.f("ix_audit_events_claim_created"),
        "audit_events",
        ["claim_id", "created_at"],
        unique=False,
    )
    op.drop_column("audit_events", "hash")
    op.drop_column("audit_events", "prev_hash")
    op.drop_column("audit_events", "reason_code")
    op.drop_column("audit_events", "outcome")
    op.drop_column("audit_events", "line_number")
    op.drop_column("audit_events", "rule_name")
    op.drop_column("audit_events", "correlation_id")
    op.drop_column("audit_events", "seq")
    op.execute("DROP FUNCTION audit_event_hash(audit_events)")
