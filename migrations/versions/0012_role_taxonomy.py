"""role taxonomy: tenant_owner/tenant_staff -> doctor/manager + is_owner/is_manager

Revision ID: 0012_role_taxonomy
Revises: 0011_launch_waitlist
Create Date: 2026-08-07 00:00:00.000000

Product decision (approved, cross-repo contract): tenant roles `tenant_owner`/
`tenant_staff` collapse into a single `doctor` role, with two new booleans carrying what
the role string used to: `is_owner` (the clinic owner — preserves every owner-only gate,
e.g. onboarding pause) and `is_manager` (a doctor who is ALSO the clinic's manager — no
backend gate of its own yet, just stored/exposed). `manager` is a NEW role, semantically
the clinic's gestor, given every gate a `doctor` gets (see `api/deps.require_doctor`).
`admin` (the platform role) is untouched.

Safe-deploy sequence for THIS repo (migrations are a MANUAL alembic step here, never
automatic on deploy — see brain-api's deploy runbook):
  1. Deploy code that ACCEPTS both the new (`doctor`/`manager` + `is_owner`/`is_manager`
     claims) and the LEGACY (`tenant_owner`/`tenant_staff`, no claims) shapes — the
     application layer's *_LEGACY_* tuples in `api/deps.py` — BEFORE running this
     migration, so a token minted moments before deploy (still `tenant_owner`, still
     circulating for its ~30min TTL) keeps authenticating against old-shaped rows.
  2. Run this migration (`alembic upgrade head`) once the new code is live. It ADDS the
     two columns (default false, so every pre-existing row is unaffected until backfilled)
     then rewrites `users.role` for the two legacy values in place.
  3. New logins mint `doctor`/`manager` tokens with the boolean claims from the day the
     new code deployed; nothing further to do — the legacy acceptance path simply goes
     cold as old tokens expire (nothing to revert once that window has passed).

Backfill: every `tenant_owner` becomes a `doctor` that is BOTH the owner and (per product:
"the person who bought the clinic is trivially also its manager") a manager. Every
`tenant_staff` becomes a plain `doctor` (`is_owner`/`is_manager` stay at their column
default, false) — a purely additive downgrade of privilege boundary, since `manager`
already carried nothing `doctor` didn't.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_role_taxonomy"
down_revision: str | None = "0011_launch_waitlist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_manager", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("is_owner", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    # tenant_owner -> doctor, and both booleans true (the owner is trivially "also a
    # manager" of their own clinic).
    op.execute(
        "UPDATE users SET role = 'doctor', is_owner = true, is_manager = true "
        "WHERE role = 'tenant_owner'"
    )
    # tenant_staff -> doctor, booleans left at their column default (false/false) — a
    # staff member was never an owner or a manager under the old taxonomy either.
    op.execute("UPDATE users SET role = 'doctor' WHERE role = 'tenant_staff'")


def downgrade() -> None:
    # Approximate: a `doctor` row cannot be told apart from an originally-created
    # `manager` row (the `manager` role did not exist pre-migration, so any row on it
    # today was created AFTER this migration ran — downgrading is best-effort and loses
    # `is_manager` for any non-owner doctor that was granted it post-migration).
    op.execute("UPDATE users SET role = 'tenant_owner' WHERE role = 'doctor' AND is_owner")
    op.execute("UPDATE users SET role = 'tenant_staff' WHERE role = 'doctor' AND NOT is_owner")
    op.execute("UPDATE users SET role = 'tenant_staff' WHERE role = 'manager' AND NOT is_owner")
    op.execute("UPDATE users SET role = 'tenant_owner' WHERE role = 'manager' AND is_owner")

    op.drop_column("users", "is_owner")
    op.drop_column("users", "is_manager")
