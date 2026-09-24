"""tenants: brain_message_enabled default flips to true

Revision ID: 0023_brain_message_default_on
Revises: 0022_pending_identity_inline_otp
Create Date: 2026-09-23 00:00:00.000000

0017_message_channels made `brain_message_enabled` an opt-in flag, born `false`, meant to
be turned on per tenant as a clinic gradually migrated from WhatsApp to Brain-Message. In
practice nothing outside a direct DB edit ever set it to `true` — no signup path, no admin
action existed to flip it — so every tenant that was not born through the Stripe webhook /
`/doctor/onboarding` self-heal (`services/onboarding_sync.py::ensure_secretaria_provisioned`,
which never touches this column anyway) was permanently locked out of its own Brain-Message
Portal login: `GET /entitlements` reported `channels.brain_message=false`, and
`Brain-Message-Frontend` sent every one of that clinic's users to
`/403?motivo=modulo_nao_contratado`.

Product decision (dono, 2026-09-23): Brain-Message becomes the unified Portal every
tenant/doctor gets by default, not a separately-provisioned add-on. `whatsapp_enabled` is
untouched — it stays opt-in and independent; a clinic can use both channels, or just
WhatsApp, or just the Portal. Only the DEFAULT and the DATA change here; the column, its
projection into `GET /entitlements` (`channels.brain_message`), and every consumer that
gates on it (`Brain-Message-Frontend/lib/access.ts`, `services/message_switchboard.py`,
`services/portal/patient_access.py`) are unchanged — flipping the flag now is exactly the
signal all of those already know how to honor.

Backfill is unconditional (every existing tenant, not filtered by `connected_at` or any
entitlement) — unlike 0017's WhatsApp backfill, which only marked tenants with PROOF a
number was connected: there is no equivalent "proof" for Brain-Message, because the flag
was never wired to be provable. This migration is what stands in for that provisioning
step, once, for every tenant already in the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023_brain_message_default_on"
down_revision: str | None = "0022_pending_identity_inline_otp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "tenants",
        "brain_message_enabled",
        server_default=sa.text("true"),
    )
    op.execute("UPDATE tenants SET brain_message_enabled = true")


def downgrade() -> None:
    # Schema-only revert — restores the opt-in default for any tenant created after a
    # rollback. Does not un-backfill existing rows, matching this repo's convention of
    # never reversing a data backfill in `downgrade()` (see 0017's own downgrade, which
    # only drops columns and never restores pre-backfill values either).
    op.alter_column(
        "tenants",
        "brain_message_enabled",
        server_default=sa.text("false"),
    )
