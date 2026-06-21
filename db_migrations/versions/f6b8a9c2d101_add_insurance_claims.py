"""add insurance claims module

Revision ID: f6b8a9c2d101
Revises: e91a4f5c1021
Create Date: 2026-06-21 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "f6b8a9c2d101"
down_revision = "e91a4f5c1021"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "insurance_claim",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("insurer_name", sa.String(length=120), nullable=False),
        sa.Column("policy_number", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="draft"),
        sa.Column("verified_by_id", sa.Integer(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("submitted_to_officer_at", sa.DateTime(), nullable=True),
        sa.Column("officer_id", sa.Integer(), nullable=True),
        sa.Column("submitted_to_insurance_at", sa.DateTime(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("expected_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("paid_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("insurer_reference", sa.String(length=120), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("reconciliation_notes", sa.Text(), nullable=True),
        sa.Column("follow_up_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoice.id"]),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.id"]),
        sa.ForeignKeyConstraint(["verified_by_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["officer_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invoice_id"),
    )
    op.create_index(op.f("ix_insurance_claim_created_at"), "insurance_claim", ["created_at"], unique=False)
    op.create_index(op.f("ix_insurance_claim_insurer_name"), "insurance_claim", ["insurer_name"], unique=False)
    op.create_index(op.f("ix_insurance_claim_invoice_id"), "insurance_claim", ["invoice_id"], unique=True)
    op.create_index(op.f("ix_insurance_claim_officer_id"), "insurance_claim", ["officer_id"], unique=False)
    op.create_index(op.f("ix_insurance_claim_patient_id"), "insurance_claim", ["patient_id"], unique=False)
    op.create_index(op.f("ix_insurance_claim_status"), "insurance_claim", ["status"], unique=False)
    op.create_index(op.f("ix_insurance_claim_verified_by_id"), "insurance_claim", ["verified_by_id"], unique=False)


def downgrade():
    op.drop_table("insurance_claim")
