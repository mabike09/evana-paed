"""add visit seen by clinician

Revision ID: 2b8e1c4d9a01
Revises: 5a8d2f1c9b44
Create Date: 2026-07-11 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "2b8e1c4d9a01"
down_revision = "5a8d2f1c9b44"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("visit") as batch_op:
        batch_op.add_column(sa.Column("seen_by_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_visit_seen_by_id", ["seen_by_id"], unique=False)
        batch_op.create_foreign_key("fk_visit_seen_by_id_user", "user", ["seen_by_id"], ["id"])


def downgrade():
    with op.batch_alter_table("visit") as batch_op:
        batch_op.drop_constraint("fk_visit_seen_by_id_user", type_="foreignkey")
        batch_op.drop_index("ix_visit_seen_by_id")
        batch_op.drop_column("seen_by_id")
