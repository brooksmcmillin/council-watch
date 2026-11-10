"""Track successful recipients for the current document revision.

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
"""

import sqlalchemy as sa

from alembic import op

revision: str = "a6b7c8d9e0f1"  # pragma: allowlist secret
down_revision: str = "f5a6b7c8d9e0"  # pragma: allowlist secret
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Existing completed notifications remain complete; historical recipients
    # cannot be reconstructed and must not be resent during migration.
    op.add_column(
        "documents",
        sa.Column("email_delivered_to", sa.JSON(), server_default="[]", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("documents", "email_delivered_to")
