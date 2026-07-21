"""cancel_scheduled_at column for trial-expiry cancellation (billing meter pricing round)

Revision ID: 0008_billing_meter_pricing
Revises: 0007_onboarding_state_machine
Create Date: 2026-07-19 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_billing_meter_pricing"
down_revision: str | None = "0007_onboarding_state_machine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- entitlements: Stripe trial-expiry cancellation marker (CONTRACT_onboarding_v1.md
    # §9; fully-metered secretaria_ferro billing round) — set by the
    # customer.subscription.trial_will_end handler when it schedules a Stripe cancel_at
    # for a tenant that never activated; cleared by harden_charge on activation. -------
    op.add_column(
        "entitlements",
        sa.Column("cancel_scheduled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("entitlements", "cancel_scheduled_at")
