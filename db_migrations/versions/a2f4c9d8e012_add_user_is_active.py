"""add user active flag

Revision ID: a2f4c9d8e012
Revises: c3f21e9ab001, f6b8a9c2d101
Create Date: 2026-06-22 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a2f4c9d8e012"
down_revision = ("c3f21e9ab001", "f6b8a9c2d101")
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.alter_column("is_active", server_default=None)


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("is_active")
