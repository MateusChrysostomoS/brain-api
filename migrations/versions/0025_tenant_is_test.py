"""tenants.is_test: clinics created by an admin for testing, outside Stripe

Revision ID: 0025_tenant_is_test
Revises: 0024_message_patient_name
Create Date: 2026-09-25 00:00:00.000000

`POST /admin/tenants` creates test clinics with no Stripe link (owner, 2026-09-24). The
flag lets brain-api refuse every tenant-initiated Stripe action for them and ignore any
webhook naming them, so a test clinic can never become a billed one
(`docs/CHECKPOINT_admin_test_tenant.md`).

Purely additive, NOT NULL with server default false: every existing tenant is a real one.
Must run BEFORE the brain-api that maps the column (the ORM selects it on every tenant
read). TEMPORARY: drop it together with the endpoint before the real launch.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025_tenant_is_test"
down_revision: str | None = "0024_message_patient_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("is_test", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("tenants", "is_test")
