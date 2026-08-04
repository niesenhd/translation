"""Reserve the exact username ``admin`` for the local administrator.

Revision ID: 20260804_0003
Revises: 20260804_0002
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "20260804_0003"
down_revision: str | None = "20260804_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Deliberately leave password_hash and profile fields untouched.  The
    # existing local credential is the only safe password source for this
    # reserved account; a migration must never invent or replace it.
    op.execute(
        """
        UPDATE users
        SET auth_source = 'local',
            oa_id = NULL,
            active_override = NULL,
            is_admin = TRUE,
            is_active = TRUE
        WHERE username = 'admin'
        """
    )


def downgrade() -> None:
    # The prior OA binding and status cannot be reconstructed safely.  A
    # downgrade therefore keeps the reserved local administrator intact.
    pass
