"""Migration 0026 — entitlements.precheck_plan / manual_products (TASK C, spec §5.11).

Hermetic: loads the migration FILE and runs its `upgrade()` against an OLD-shaped
`entitlements` table on in-memory SQLite (the suite's `create_all` proves nothing about a
migration). The Postgres run against a disposable database is in the proof checklist of
docs/CHECKPOINT_billing_add_product.md.
"""

import importlib.util
import json
import pathlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from brain_api.models import Entitlement

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0026_entitlement_product_state.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("mig_0026", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _as_list(value) -> list:
    return json.loads(value) if isinstance(value, str) else value


def test_revision_chain_and_id_length():
    module = _load()
    assert module.revision == "0026_entitlement_product_state"
    assert module.down_revision == "0025_tenant_is_test"
    # alembic_version.version_num is VARCHAR(32): a longer id fails on Postgres.
    assert len(module.revision) <= 32


def test_model_declares_both_columns():
    table = Entitlement.__table__
    assert table.c.precheck_plan.nullable is True
    assert table.c.precheck_plan.type.length == 32
    assert table.c.manual_products.nullable is False
    assert table.c.manual_products.server_default is not None


def test_upgrade_adds_columns_and_backfills_manual_products():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE entitlements ("
                "tenant_id VARCHAR(36) PRIMARY KEY, "
                "precheck_enabled BOOLEAN NOT NULL DEFAULT 0, "
                "secretaria_enabled BOOLEAN NOT NULL DEFAULT 0, "
                "stripe_subscription_id VARCHAR(64))"
            )
        )
        rows = [
            ("t-manual-precheck", 1, 0, None),
            ("t-manual-secretaria", 0, 1, None),
            ("t-manual-both", 1, 1, None),
            ("t-paid", 1, 0, "sub_1"),  # has a subscription: NOT manual
            ("t-inert", 0, 0, None),  # signup entitlement, nothing on
        ]
        for tenant_id, precheck, secretaria, sub in rows:
            conn.execute(
                sa.text(
                    "INSERT INTO entitlements (tenant_id, precheck_enabled, secretaria_enabled, "
                    "stripe_subscription_id) VALUES (:t, :p, :s, :sub)"
                ),
                {"t": tenant_id, "p": precheck, "s": secretaria, "sub": sub},
            )

        with Operations.context(MigrationContext.configure(conn)):
            _load().upgrade()

        result = {
            row[0]: (row[1], _as_list(row[2]))
            for row in conn.execute(
                sa.text("SELECT tenant_id, precheck_plan, manual_products FROM entitlements")
            ).all()
        }

    assert result["t-manual-precheck"] == (None, ["precheck"])
    assert result["t-manual-secretaria"] == (None, ["secretaria"])
    assert result["t-manual-both"] == (None, ["precheck", "secretaria"])
    assert result["t-paid"] == (None, [])
    assert result["t-inert"] == (None, [])
