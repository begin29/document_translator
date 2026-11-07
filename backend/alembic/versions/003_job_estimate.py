"""job estimate stored at creation

Revision ID: 003
Revises: 002
Create Date: 2026-11-07
"""

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE translation_jobs ADD COLUMN estimate_cents NUMERIC(14, 4)")


def downgrade() -> None:
    op.execute("ALTER TABLE translation_jobs DROP COLUMN estimate_cents")
