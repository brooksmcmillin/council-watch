"""add revised tracking

Revision ID: b1f2c3d4e5a6
Revises: 8add4a71d3d1
Create Date: 2026-07-03 13:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1f2c3d4e5a6"  # pragma: allowlist secret
down_revision: str | None = "8add4a71d3d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("revised_at", sa.DateTime(), nullable=True))
    op.add_column(
        "scrape_log",
        sa.Column(
            "revised_documents",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("scrape_log", "revised_documents")
    op.drop_column("documents", "revised_at")
