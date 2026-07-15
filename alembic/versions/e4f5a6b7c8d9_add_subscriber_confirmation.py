"""add subscriber confirmation

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-07-14 00:00:00.000000
"""

import secrets
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4f5a6b7c8d9"  # pragma: allowlist secret
down_revision: str | None = "d3e4f5a6b7c8"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscribers",
        sa.Column("confirmation_token", sa.Text(), nullable=True),
    )
    op.add_column(
        "subscribers",
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    subscribers = sa.table(
        "subscribers",
        sa.column("id", sa.Integer()),
        sa.column("confirmation_token", sa.Text()),
    )
    connection = op.get_bind()
    ids = connection.execute(sa.select(subscribers.c.id)).scalars()
    for subscriber_id in ids:
        connection.execute(
            subscribers.update()
            .where(subscribers.c.id == subscriber_id)
            .values(confirmation_token=secrets.token_urlsafe(32))
        )

    with op.batch_alter_table("subscribers") as batch_op:
        batch_op.alter_column("confirmation_token", nullable=False)
        batch_op.create_unique_constraint(
            "uq_subscribers_confirmation_token", ["confirmation_token"]
        )
        batch_op.alter_column("confirmed", server_default=sa.false())


def downgrade() -> None:
    with op.batch_alter_table("subscribers") as batch_op:
        batch_op.drop_constraint("uq_subscribers_confirmation_token", type_="unique")
        batch_op.drop_column("confirmed")
        batch_op.drop_column("confirmation_token")
