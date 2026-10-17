"""adjudication tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17 00:04:50.223037

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Generated with --autogenerate, then reviewed by hand:
    # - removed duplicated status/outcome CHECKs (the Enum columns already create them)
    op.create_table(
        "fee_schedule",
        sa.Column("procedure_code", sa.String(length=5), nullable=False),
        sa.Column("rate_per_unit", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("max_units_per_day", sa.Integer(), nullable=False),
        sa.CheckConstraint("max_units_per_day > 0", name="max_units_positive"),
        sa.CheckConstraint("rate_per_unit >= 0", name="rate_non_negative"),
        sa.ForeignKeyConstraint(
            ["procedure_code"],
            ["cpt_codes.code"],
        ),
        sa.PrimaryKeyConstraint("procedure_code"),
    )
    op.create_table(
        "member_accumulators",
        sa.Column("member_id", sa.String(length=20), nullable=False),
        sa.Column("plan_year", sa.Integer(), nullable=False),
        sa.Column("deductible_met", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("out_of_pocket_met", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["members.id"],
        ),
        sa.PrimaryKeyConstraint("member_id", "plan_year"),
    )
    op.create_table(
        "plan_benefits",
        sa.Column("plan_id", sa.String(length=20), nullable=False),
        sa.Column("procedure_code", sa.String(length=5), nullable=False),
        sa.Column("covered", sa.Boolean(), nullable=False),
        sa.Column("requires_prior_auth", sa.Boolean(), nullable=False),
        sa.Column("copay", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.CheckConstraint("copay IS NULL OR copay >= 0", name="copay_non_negative"),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["plans.id"],
        ),
        sa.ForeignKeyConstraint(
            ["procedure_code"],
            ["cpt_codes.code"],
        ),
        sa.PrimaryKeyConstraint("plan_id", "procedure_code"),
    )
    op.create_table(
        "prior_authorizations",
        sa.Column("auth_number", sa.String(length=20), nullable=False),
        sa.Column("member_id", sa.String(length=20), nullable=False),
        sa.Column("procedure_code", sa.String(length=5), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "APPROVED",
                "PENDING",
                "DENIED",
                name="auth_status",
                native_enum=False,
                create_constraint=True,
                length=10,
            ),
            nullable=False,
        ),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=False),
        sa.CheckConstraint("valid_to >= valid_from", name="auth_valid_range"),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["members.id"],
        ),
        sa.ForeignKeyConstraint(
            ["procedure_code"],
            ["cpt_codes.code"],
        ),
        sa.PrimaryKeyConstraint("auth_number"),
    )
    op.create_index(
        "ix_prior_auths_member_code",
        "prior_authorizations",
        ["member_id", "procedure_code"],
        unique=False,
    )
    op.create_table(
        "claim_line_adjudications",
        sa.Column("claim_line_id", sa.Integer(), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                "APPROVED",
                "DENIED",
                "PENDED",
                name="line_outcome",
                native_enum=False,
                create_constraint=True,
                length=10,
            ),
            nullable=False,
        ),
        sa.Column("decided_by", sa.String(length=40), nullable=False),
        sa.Column("carc", sa.String(length=5), nullable=True),
        sa.Column("message", sa.String(length=300), nullable=False),
        sa.Column("allowed", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("contractual_adjustment", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("deductible", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("copay", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("coinsurance", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("patient_responsibility", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("paid", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("allowed = paid + patient_responsibility", name="allowed_reconciles"),
        sa.CheckConstraint(
            "patient_responsibility = deductible + copay + coinsurance",
            name="patient_share_reconciles",
        ),
        sa.ForeignKeyConstraint(["claim_line_id"], ["claim_lines.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("claim_line_id"),
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table("claim_line_adjudications")
    op.drop_index("ix_prior_auths_member_code", table_name="prior_authorizations")
    op.drop_table("prior_authorizations")
    op.drop_table("plan_benefits")
    op.drop_table("member_accumulators")
    op.drop_table("fee_schedule")
    # ### end Alembic commands ###
