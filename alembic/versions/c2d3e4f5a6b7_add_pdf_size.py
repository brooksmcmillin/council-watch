"""add pdf_size

Revision ID: c2d3e4f5a6b7
Revises: b1f2c3d4e5a6
Create Date: 2026-07-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"  # pragma: allowlist secret
down_revision: str | None = "b1f2c3d4e5a6"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("pdf_size", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "pdf_size")
