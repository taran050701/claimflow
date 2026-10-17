"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-16 23:44:43.466769

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Generated with --autogenerate, then reviewed by hand:
    # - removed a duplicated status CHECK (the Enum column already creates it)
    # - audit_events.id widened to BIGINT (append-only, grows forever)
    op.create_table(
        "claims",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.String(length=20), nullable=False),
        sa.Column("provider_npi", sa.String(length=10), nullable=False),
        sa.Column("date_of_service", sa.Date(), nullable=False),
        sa.Column("diagnosis_codes", postgresql.ARRAY(sa.String(length=10)), nullable=False),
        sa.Column("total_billed", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "RECEIVED",
                "VALIDATED",
                "REJECTED",
                "PENDED",
                "ADJUDICATED",
                "PAID",
                "DENIED",
                "APPEALED",
                name="claim_status",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=100), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_claims_member_dos", "claims", ["member_id", "date_of_service"], unique=False
    )
    op.create_index("ix_claims_status", "claims", ["status"], unique=False)
    op.create_table(
        "members",
        sa.Column("id", sa.String(length=20), nullable=False),
        sa.Column("first_name", sa.String(length=50), nullable=False),
        sa.Column("last_name", sa.String(length=50), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "plans",
        sa.Column("id", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("annual_deductible", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("coinsurance_rate", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("out_of_pocket_max", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.CheckConstraint("annual_deductible >= 0", name="deductible_non_negative"),
        sa.CheckConstraint("coinsurance_rate BETWEEN 0 AND 1", name="coinsurance_is_fraction"),
        sa.CheckConstraint("out_of_pocket_max >= annual_deductible", name="oop_max_gte_deductible"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "providers",
        sa.Column("npi", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("specialty", sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint("npi"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_claim_created", "audit_events", ["claim_id", "created_at"], unique=False
    )
    op.create_table(
        "claim_lines",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("procedure_code", sa.String(length=10), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("billed_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("claim_id", "line_number", name="uq_claim_line_number"),
    )
    op.create_table(
        "member_coverages",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("member_id", sa.String(length=20), nullable=False),
        sa.Column("plan_id", sa.String(length=20), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.CheckConstraint("end_date IS NULL OR end_date >= start_date", name="valid_date_range"),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["members.id"],
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["plans.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_member_coverages_member_start",
        "member_coverages",
        ["member_id", "start_date"],
        unique=False,
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index("ix_member_coverages_member_start", table_name="member_coverages")
    op.drop_table("member_coverages")
    op.drop_table("claim_lines")
    op.drop_index("ix_audit_events_claim_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("providers")
    op.drop_table("plans")
    op.drop_table("members")
    op.drop_index("ix_claims_status", table_name="claims")
    op.drop_index("ix_claims_member_dos", table_name="claims")
    op.drop_table("claims")
    # ### end Alembic commands ###
