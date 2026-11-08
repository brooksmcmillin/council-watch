"""Add extracted HTML document text.

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
"""

import sqlalchemy as sa

from alembic import op

revision: str = "f5a6b7c8d9e0"  # pragma: allowlist secret
down_revision: str = "e4f5a6b7c8d9"  # pragma: allowlist secret
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("text_content", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "text_content")
