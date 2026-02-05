from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "xxxx_pendingpayment"
down_revision = "7c17a2deaff9"
branch_labels = None
depends_on = None

def upgrade():
    # SQLite cannot ALTER COLUMN to set DEFAULT.
    # This migration is intentionally a no-op; application-level default handles it.
    pass


def downgrade():
    pass
