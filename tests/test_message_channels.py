"""Delivery-channel columns on `tenants` + their `EntitlementOut` projection.

The Brain-Message round's piece 1: until now the channel did not exist in the data
model — every tenant was WhatsApp by omission, because there was no other channel.
`tenants.whatsapp_enabled` / `brain_message_enabled` (migration 0017_message_channels)
make it answerable, and `GET /entitlements` carries the answer to the portal.

Four things are worth pinning down, and each has its own failure mode:
- The two columns default OFF — both at the ORM layer and in the DDL's `server_default`,
  because the backfill statement runs as raw SQL against rows the ORM never touched.
- They are INDEPENDENT (a set, like `products`), not an exclusive enum: a clinic
  migrating gradually is both at once, and a regression to an enum would break exactly
  this assertion.
- The backfill predicate is `connected_at IS NOT NULL` and nothing else — a tenant still
  in onboarding must NOT come out marked as being on a channel it never reached.
- `channels` is resolved from the TENANT row, so it is correct even for a tenant with no
  `entitlements` row at all (the default-resolution branch).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from brain_api.models import Entitlement, Tenant
from brain_api.services.entitlements import resolve_entitlement
from tests.test_rbac import (
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    OWNER_B_EMAIL,
    OWNER_B_PASSWORD,
    _bearer,
    _token,
)

# The exact statement migration 0017 runs, replayed here so the predicate itself is
# covered by a test rather than only by the one-shot production run.
BACKFILL_SQL = "UPDATE tenants SET whatsapp_enabled = true WHERE connected_at IS NOT NULL"


# ---------------------------------------------------------------------------
# 1) Defaults — ORM layer and DDL layer
# ---------------------------------------------------------------------------


async def test_new_tenant_has_both_channels_off(db_session):
    """A tenant created through the ORM is born with no channel enabled."""
    tenant = Tenant(clinic_name="Clínica Sem Canal")
    db_session.add(tenant)
    await db_session.commit()
    await db_session.refresh(tenant)

    assert tenant.whatsapp_enabled is False
    assert tenant.brain_message_enabled is False


async def test_server_default_covers_rows_the_orm_never_touched(db_session):
    """A raw INSERT that never mentions the columns still lands `false`/`false`.

    This is the layer that matters for the migration: `NOT NULL DEFAULT false` is what
    keeps `op.add_column` from failing on a populated `tenants` table, and what the raw
    backfill UPDATE reads before it writes.
    """
    raw_id = uuid.uuid4()
    await db_session.execute(
        text("INSERT INTO tenants (id, clinic_name) VALUES (:id, :name)"),
        {"id": raw_id.hex, "name": "Clínica Crua"},
    )
    await db_session.commit()

    tenant = await db_session.get(Tenant, raw_id)
    assert tenant is not None
    assert tenant.whatsapp_enabled is False
    assert tenant.brain_message_enabled is False


# ---------------------------------------------------------------------------
# 2) The backfill predicate
# ---------------------------------------------------------------------------


async def test_backfill_marks_only_tenants_with_a_connected_number(db_session):
    """`connected_at IS NOT NULL` is the whole predicate.

    `services/onboarding.py::record_attempt` stamps `connected_at` on a 'pass' attempt —
    it is the only signal in this repo that a WABA was actually connected. A tenant still
    in onboarding must come out untouched: marking it would invent a channel it does not
    have, and `brain_message_enabled` must not move for ANYONE.
    """
    connected = Tenant(clinic_name="Clínica Conectada", connected_at=datetime.now(UTC))
    never_connected = Tenant(clinic_name="Clínica Em Onboarding")
    db_session.add_all([connected, never_connected])
    await db_session.commit()

    await db_session.execute(text(BACKFILL_SQL))
    await db_session.commit()
    await db_session.refresh(connected)
    await db_session.refresh(never_connected)

    assert connected.whatsapp_enabled is True
    assert never_connected.whatsapp_enabled is False
    # The backfill touches ONE column: nobody is born on Brain-Message.
    assert connected.brain_message_enabled is False
    assert never_connected.brain_message_enabled is False


# ---------------------------------------------------------------------------
# 3) EntitlementOut projection
# ---------------------------------------------------------------------------


async def test_resolve_entitlement_reports_channels_without_an_entitlement_row(db_session):
    """The default-resolution branch still tells the truth about the channel.

    Channel lives on `tenants`, plan lives on `entitlements` — a tenant that has never
    been billed can still be on WhatsApp, and the portal has to see that.
    """
    tenant = Tenant(clinic_name="Clínica Sem Entitlement", whatsapp_enabled=True)
    db_session.add(tenant)
    await db_session.commit()

    out = await resolve_entitlement(db_session, tenant.id)

    assert out.plan == "free"
    assert out.status == "inactive"
    assert out.channels.whatsapp is True
    assert out.channels.brain_message is False


async def test_channels_are_a_set_not_an_exclusive_choice(db_session):
    """Both channels on at once — the gradual-migration case the two booleans exist for."""
    tenant = Tenant(
        clinic_name="Clínica Migrando", whatsapp_enabled=True, brain_message_enabled=True
    )
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        Entitlement(
            tenant_id=tenant.id,
            precheck_enabled=True,
            secretaria_enabled=True,
            plan="complete_clinic_combo",
            status="active",
        )
    )
    await db_session.commit()

    out = await resolve_entitlement(db_session, tenant.id)

    assert out.channels.whatsapp is True
    assert out.channels.brain_message is True
    # The channel says nothing about the plan, and vice versa.
    assert out.products.precheck is True
    assert out.products.secretaria is True


async def test_only_brain_message_is_a_valid_state(db_session):
    """A clinic sold Brain-Message with no WhatsApp at all — the point of the round."""
    tenant = Tenant(clinic_name="Clínica Sem WhatsApp", brain_message_enabled=True)
    db_session.add(tenant)
    await db_session.commit()

    out = await resolve_entitlement(db_session, tenant.id)

    assert out.channels.whatsapp is False
    assert out.channels.brain_message is True


async def test_missing_tenant_row_degrades_to_both_channels_off(db_session):
    """Should not happen for a valid token; if it does, fail closed instead of crashing —
    the same rule `clinic_name=""` already follows."""
    out = await resolve_entitlement(db_session, uuid.uuid4())

    assert out.clinic_name == ""
    assert out.channels.whatsapp is False
    assert out.channels.brain_message is False


# ---------------------------------------------------------------------------
# 4) The HTTP surface (CONTRACTS.md 3.1)
# ---------------------------------------------------------------------------


async def test_get_entitlements_serializes_channels(client):
    """`GET /entitlements` carries `channels` for both the entitled tenant (A) and the
    one with no entitlement row (B) — neither branch may omit the key."""
    for email, password in ((OWNER_A_EMAIL, OWNER_A_PASSWORD), (OWNER_B_EMAIL, OWNER_B_PASSWORD)):
        token = await _token(client, email, password)
        resp = await client.get("/entitlements", headers=_bearer(token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["channels"] == {"whatsapp": False, "brain_message": False}
        # The additive field did not disturb what the frontend already consumes.
        assert set(body["products"]) == {"precheck", "secretaria"}
