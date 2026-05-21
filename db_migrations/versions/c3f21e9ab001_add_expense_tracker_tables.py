"""add expense tracker tables

Revision ID: c3f21e9ab001
Revises: d4b8c9f1a001
Create Date: 2026-05-21
"""

from alembic import op
import sqlalchemy as sa


revision = "c3f21e9ab001"
down_revision = "d4b8c9f1a001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "expense_category",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=150), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_expense_category_name", "expense_category", ["name"], unique=True)
    op.create_index("ix_expense_category_is_active", "expense_category", ["is_active"], unique=False)
    op.create_index("ix_expense_category_created_at", "expense_category", ["created_at"], unique=False)

    op.create_table(
        "expense_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("expense_date", sa.String(length=10), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("vendor_payee", sa.String(length=180), nullable=False),
        sa.Column("reference", sa.String(length=100), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("entered_by", sa.String(length=150), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["expense_category.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_expense_entry_expense_date", "expense_entry", ["expense_date"], unique=False)
    op.create_index("ix_expense_entry_category_id", "expense_entry", ["category_id"], unique=False)
    op.create_index("ix_expense_entry_vendor_payee", "expense_entry", ["vendor_payee"], unique=False)
    op.create_index("ix_expense_entry_reference", "expense_entry", ["reference"], unique=False)
    op.create_index("ix_expense_entry_entered_by", "expense_entry", ["entered_by"], unique=False)
    op.create_index("ix_expense_entry_created_at", "expense_entry", ["created_at"], unique=False)


def downgrade():
    op.drop_index("ix_expense_entry_created_at", table_name="expense_entry")
    op.drop_index("ix_expense_entry_entered_by", table_name="expense_entry")
    op.drop_index("ix_expense_entry_reference", table_name="expense_entry")
    op.drop_index("ix_expense_entry_vendor_payee", table_name="expense_entry")
    op.drop_index("ix_expense_entry_category_id", table_name="expense_entry")
    op.drop_index("ix_expense_entry_expense_date", table_name="expense_entry")
    op.drop_table("expense_entry")

    op.drop_index("ix_expense_category_created_at", table_name="expense_category")
    op.drop_index("ix_expense_category_is_active", table_name="expense_category")
    op.drop_index("ix_expense_category_name", table_name="expense_category")
    op.drop_table("expense_category")
