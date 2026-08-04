"""Add the administrator override for OA user activation.

Revision ID: 20260804_0002
Revises: 20260804_0001
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260804_0002"
down_revision: str | None = "20260804_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("active_override", sa.Boolean(), nullable=True),
    )
    # Before this column existed, OA sync deliberately updated oa_employed but
    # never overwrote is_active.  A difference therefore represents an
    # administrator decision and must survive the migration in either
    # direction (force-enabled departed user or force-disabled employed user).
    op.execute(
        """
        UPDATE users
        SET active_override = CASE
                WHEN is_active IS DISTINCT FROM oa_employed THEN is_active
                ELSE NULL
            END
        WHERE (auth_source = 'oa' OR oa_id IS NOT NULL)
        """
    )


def downgrade() -> None:
    op.drop_column("users", "active_override")
