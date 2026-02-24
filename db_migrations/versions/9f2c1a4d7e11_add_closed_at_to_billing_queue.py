"""add_closed_at_to_billing_queue

Revision ID: 9f2c1a4d7e11
Revises: xxxx_pendingpayment
Create Date: 2026-02-24 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9f2c1a4d7e11"
down_revision = "xxxx_pendingpayment"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("billing_queue") as batch_op:
        batch_op.add_column(sa.Column("closed_at", sa.DateTime(), nullable=True))
        batch_op.create_index("ix_billing_queue_closed_at", ["closed_at"], unique=False)


def downgrade():
    with op.batch_alter_table("billing_queue") as batch_op:
        batch_op.drop_index("ix_billing_queue_closed_at")
        batch_op.drop_column("closed_at")
