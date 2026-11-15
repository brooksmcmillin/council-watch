"""Remember the unchanged source of a provider-refused summary.

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
"""

import sqlalchemy as sa

from alembic import op

revision: str = "b7c8d9e0f1a2"  # pragma: allowlist secret
down_revision: str = "a6b7c8d9e0f1"  # pragma: allowlist secret
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("summary_refused_hash", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "summary_refused_hash")
