"""secretary role: add `secretary` to the tenant role taxonomy (NO-OP by design)

Revision ID: 0013_secretary_role
Revises: 0012_role_taxonomy
Create Date: 2026-08-14 00:00:00.000000

Product decision (approved): a 4th role, `secretary` — the clinic's HUMAN receptionist —
joins `admin`/`doctor`/`manager`. secretarIA-only (never PreCheck/clinical), full power
inside secretarIA (every professional's agenda, the whole configuracao surface, team
management, billing, and the onboarding pause), and never a professional itself
(`professional_id` stays NULL, so a secretary is never bookable).

WHY `upgrade()` IS EMPTY — this is a deliberate no-op, not an unfinished stub:

  * `users.role` is a plain `sa.String(length=32)` (see 0001_initial_schema.py, the
    `role` column) — there is NO native Postgres enum and NO check constraint on it
    anywhere in this repo's migration history. Role values are validated purely at the
    application layer (`models/user.ROLES`, `api/deps.py`'s role tuples, and the
    `Literal[...]` on `schemas/admin.AdminUserCreateIn`). So a new role STRING needs no
    DDL: the database already accepts it.
  * There is nothing to backfill either. 0012 had to rewrite rows because it RENAMED two
    existing role values (`tenant_owner`/`tenant_staff` -> `doctor`) and added two
    columns; `secretary` is purely additive — no row in `users` should become a secretary
    retroactively. New secretaries are created going forward by
    `POST /doctor/secretaries/invites` (api/onboarding.py) and by admin tooling.

This revision exists so the role taxonomy change is visible in the migration history at
the same place someone reviewing 0012 would look, and so the deploy checklist has an
explicit "nothing to run" answer instead of an ambiguous silence. Running it is safe and
instantaneous; skipping it changes no behaviour whatsoever.

IF a future round adds a DB-level constraint on `users.role` (enum or CHECK), that
migration — not this one — must include `secretary` in the allowed set.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0013_secretary_role"
down_revision: str | None = "0012_role_taxonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Intentionally empty — see the module docstring."""


def downgrade() -> None:
    """Intentionally empty.

    Deliberately does NOT delete or rewrite `secretary` rows: a downgrade is a schema
    rollback, and silently destroying (or reassigning to `doctor`, which would GRANT
    clinical access the role is defined not to have) real user accounts is far worse than
    leaving rows whose role string the older code simply fails closed on — an older
    `require_doctor` does not list `secretary`, so those users get 403 until the code is
    rolled forward again. No data is lost either way.
    """
