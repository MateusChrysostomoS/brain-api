"""password reset: single-use reset token columns on `users`

Revision ID: 0014_password_reset
Revises: 0013_secretary_role
Create Date: 2026-08-14 00:00:00.000000

brain-api had NO password-reset capability at all before this revision — its auth surface
was `/token`, `/refresh`, `/logout`, `/me`, the two token exchanges and `/set-password`
(which is not a reset: it requires an already-authenticated session). The "Esqueci a
senha" screens in brain-frontend were calling PreCheck's API instead, which silently did
nothing for any user that exists only in brain-api (i.e. every self-serve signup).

Purely ADDITIVE and nullable, so it is safe to run against a live database with no
downtime and no backfill: existing rows simply have no pending reset. Same risk profile
as 0013.

The columns deliberately mirror `invite_token_hash` / `invite_token_expires_at` (added in
0007) — same `String(64)` sha256-at-rest scheme, same index, same "burn on redemption"
semantics — rather than introducing a separate `password_reset_tokens` table like
PreCheck has. One pending reset per user is the whole requirement, and overwriting the
hash invalidates the previous link for free.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_password_reset"
down_revision: str | None = "0013_secretary_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("reset_token_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "users",
        sa.Column("reset_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Looked up by hash on every verify/confirm — same index as the invite token.
    op.create_index(
        op.f("ix_users_reset_token_hash"), "users", ["reset_token_hash"], unique=False
    )


def downgrade() -> None:
    """Drops the columns. Any pending reset links become permanently unusable, which is
    the correct failure mode for a rollback — a reset token is short-lived (30 min) and
    re-requestable, so nothing of value is lost and no password is affected."""
    op.drop_index(op.f("ix_users_reset_token_hash"), table_name="users")
    op.drop_column("users", "reset_token_expires_at")
    op.drop_column("users", "reset_token_hash")
