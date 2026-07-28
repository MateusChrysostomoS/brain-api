"""tenants test-window columns (Task 2: reframe the free trial as a Meta/WABA acceptance
test window)

Revision ID: 0009_test_window
Revises: 0008_billing_meter_pricing
Create Date: 2026-07-22 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_test_window"
down_revision: str | None = "0008_billing_meter_pricing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- tenants: Meta/WABA acceptance test-window markers ------------------------------
    # `test_window_started_at` anchors the window (payment completion / a resubscription /
    # a manual restart, services/signup.py::provision_tenant_from_intent +
    # services/billing.py's subscription-id-change reset + POST /doctor/onboarding/
    # test-window/restart); `test_window_notified_at` is the one-shot "the D+N email was
    # sent" marker (services/onboarding_sync.py::apply_onboarding_event,
    # event="test_window_email_sent").
    op.add_column(
        "tenants", sa.Column("test_window_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "tenants", sa.Column("test_window_notified_at", sa.DateTime(timezone=True), nullable=True)
    )

    # Backfill: every pre-existing tenant starts its window at its own creation time (no
    # new tenant can have been inserted mid-migration, so targeting every NULL row is
    # safe — same reasoning as 0007_onboarding_state_machine's own backfill).
    op.execute(
        "UPDATE tenants SET test_window_started_at = created_at WHERE test_window_started_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("tenants", "test_window_notified_at")
    op.drop_column("tenants", "test_window_started_at")
