"""precheck provisioning bridge: template choice on the intent + provisioned marker

Revision ID: 0015_precheck_provisioning
Revises: 0014_password_reset
Create Date: 2026-08-24 00:00:00.000000

Until now, paying for PreCheck provisioned NOTHING in PreCheck. `apply_stripe_event`
activated the entitlement and called `ensure_secretaria_provisioned`, but there was no
PreCheck counterpart — so the buyer landed on `/checkout/sucesso` and got told "nossa
equipe entrará em contato em até 24 horas", and `precheck_account_links` (which the
`POST /sso/precheck/token` handoff REQUIRES) was populated by a manual script
(`scripts/link_precheck_account.py`). This revision adds the two columns that let the
bridge run unattended.

`tenants.precheck_provisioned_at` — the idempotency marker, exactly mirroring
`secretaria_provisioned_at` (added in 0006): NULL means "not provisioned yet, retry",
stamped means "no-op". Same fail-soft retry story: the Stripe webhook fires it once and
`GET /doctor/onboarding` lazily retries, so a PreCheck outage at purchase time self-heals.

`signup_intents.precheck_template_slug` — WHICH specialty flow the clinic gets. PreCheck's
`POST /internal/provision` requires a `template_slug` and the intent had no field carrying
one; without this every self-serve clinic would be forced onto the generic `clinica-geral`
and the 30 specialty templates would be worthless in self-service. Nullable, because the
column is only meaningful for PreCheck-bearing purchases and older intents predate it —
the bridge falls back to `clinica-geral` when it is NULL.

Purely ADDITIVE and nullable: safe against a live database, no downtime, no backfill.
Same risk profile as 0013/0014.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_precheck_provisioning"
down_revision: str | None = "0014_password_reset"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("precheck_provisioned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "signup_intents",
        sa.Column("precheck_template_slug", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("signup_intents", "precheck_template_slug")
    op.drop_column("tenants", "precheck_provisioned_at")
