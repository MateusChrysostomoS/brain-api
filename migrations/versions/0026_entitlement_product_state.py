"""entitlements.precheck_plan + manual_products: two products in one subscription

Revision ID: 0026_entitlement_product_state
Revises: 0025_tenant_is_test
Create Date: 2026-09-29 00:00:00.000000

TASK C (`docs/CHECKPOINT_billing_add_product.md`): a clinic adds the OTHER product to its
existing Stripe subscription, so one entitlement row now has to describe two product
families. `precheck_plan` holds the PreCheck tier when the anchor `plan` is a secretarIA plan;
`manual_products` lists the families switched on outside Stripe (courtesy / admin), which a
subscription event must never switch off.

Purely additive: `precheck_plan` NULL, `manual_products` NOT NULL with server default `[]`,
so the previous brain-api ignores both columns and a code rollback is safe. Data backfill:
every row WITHOUT a Stripe subscription and with a product switched on gets those families in
`manual_products` (that is exactly a courtesy/admin/test clinic); rows with a subscription stay
`[]`. Must run BEFORE the brain-api that maps the columns (the ORM selects them on every
entitlement read). The revision id is <= 32 characters on purpose: `alembic_version.version_num`
is VARCHAR(32).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026_entitlement_product_state"
down_revision: str | None = "0025_tenant_is_test"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("entitlements", sa.Column("precheck_plan", sa.String(length=32), nullable=True))
    op.add_column(
        "entitlements",
        sa.Column("manual_products", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )

    bind = op.get_bind()
    entitlements = sa.table(
        "entitlements",
        sa.column("tenant_id"),
        sa.column("precheck_enabled", sa.Boolean()),
        sa.column("secretaria_enabled", sa.Boolean()),
        sa.column("stripe_subscription_id", sa.String()),
        sa.column("manual_products", sa.JSON()),
    )
    rows = bind.execute(
        sa.select(
            entitlements.c.tenant_id,
            entitlements.c.precheck_enabled,
            entitlements.c.secretaria_enabled,
        ).where(
            entitlements.c.stripe_subscription_id.is_(None),
            sa.or_(
                entitlements.c.precheck_enabled == sa.true(),
                entitlements.c.secretaria_enabled == sa.true(),
            ),
        )
    ).all()
    for tenant_id, precheck, secretaria in rows:
        families = [
            family
            for family, enabled in (("precheck", precheck), ("secretaria", secretaria))
            if enabled
        ]
        bind.execute(
            sa.update(entitlements)
            .where(entitlements.c.tenant_id == tenant_id)
            .values(manual_products=families)
        )


def downgrade() -> None:
    op.drop_column("entitlements", "manual_products")
    op.drop_column("entitlements", "precheck_plan")
