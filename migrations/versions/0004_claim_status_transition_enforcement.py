"""claim status transition enforcement

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-17 00:12:49.493728

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Frozen copy of the legal transitions AT THIS POINT IN HISTORY. Migrations must
# not import app code: if TRANSITIONS changes later, a new migration changes this
# table, and re-running old migrations still produces the old, correct state.
# tests/test_state_machine.py asserts the live table matches the domain.
TRANSITIONS = [
    ("RECEIVED", "VALIDATED"),
    ("RECEIVED", "REJECTED"),
    ("VALIDATED", "ADJUDICATED"),
    ("VALIDATED", "PENDED"),
    ("PENDED", "ADJUDICATED"),
    ("PENDED", "PENDED"),
    ("ADJUDICATED", "PAID"),
    ("ADJUDICATED", "DENIED"),
    ("DENIED", "APPEALED"),
    ("APPEALED", "ADJUDICATED"),
    ("APPEALED", "PENDED"),
]

ENFORCE_FUNCTION = """
CREATE FUNCTION enforce_claim_status_transition() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'RECEIVED' THEN
            RAISE EXCEPTION 'claims must be created in status RECEIVED, not %', NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF NEW.status <> OLD.status AND NOT EXISTS (
        SELECT 1 FROM claim_status_transitions
        WHERE from_status = OLD.status AND to_status = NEW.status
    ) THEN
        RAISE EXCEPTION 'illegal claim status transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    table = op.create_table(
        "claim_status_transitions",
        sa.Column("from_status", sa.String(length=20), nullable=False),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("from_status", "to_status"),
    )
    op.bulk_insert(table, [{"from_status": f, "to_status": t} for f, t in TRANSITIONS])

    # Defense in depth: even a hand-written UPDATE can't make an illegal transition.
    op.execute(ENFORCE_FUNCTION)
    op.execute(
        "CREATE TRIGGER claims_status_transition "
        "BEFORE INSERT OR UPDATE OF status ON claims "
        "FOR EACH ROW EXECUTE FUNCTION enforce_claim_status_transition()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER claims_status_transition ON claims")
    op.execute("DROP FUNCTION enforce_claim_status_transition()")
    op.drop_table("claim_status_transitions")
