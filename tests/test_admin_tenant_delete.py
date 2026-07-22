"""Tenant cascade-delete tests (DELETE /admin/tenants/{id}).

Two layers:
- Endpoint (over the seeded `client` fixture from test_rbac): the admin gate, a 200
  delete that makes the clinic vanish from every admin view and blocks its owner's login,
  a 404 on re-delete, and the best-effort secretaria leg reporting `skipped_unconfigured`
  (SECRETARIA_* is unset in the test env, so the cross-DB call is skipped, never networked).
- Service (`delete_tenant` over the bare in-memory `db_session`): every brain-owned child
  row is removed, while an LGPD `privacy_requests` audit row SURVIVES with `requested_by`
  nulled — proving the audit trail outlives the account it records.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select

from brain_api.models import (
    Entitlement,
    PrivacyRequest,
    RefreshToken,
    Tenant,
    UsageEvent,
    User,
)
from brain_api.models.user import ROLE_TENANT_OWNER
from brain_api.services import admin as admin_service

from tests.test_rbac import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    CLINIC_A,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    MISSING_ID,
    _bearer,
    _token,
)


# --- Endpoint ---------------------------------------------------------------


async def test_delete_tenant_requires_admin(client):
    """The delete route is behind the same admin gate as the rest of /admin/*."""
    owner_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.delete(f"/admin/tenants/{MISSING_ID}", headers=_bearer(owner_token))
    assert resp.status_code == 403
    assert (await client.delete(f"/admin/tenants/{MISSING_ID}")).status_code == 401


async def test_delete_tenant_unknown_is_404(client):
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    resp = await client.delete(f"/admin/tenants/{MISSING_ID}", headers=_bearer(admin_token))
    assert resp.status_code == 404


async def test_delete_tenant_removes_clinic_everywhere(client):
    """A deleted clinic vanishes from every admin view and its owner can no longer log in."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    # Owner A logs in first — this creates a refresh_token row that must also be swept.
    assert (
        await client.post(
            "/auth/token", json={"email": OWNER_A_EMAIL, "password": OWNER_A_PASSWORD}
        )
    ).status_code == 200

    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    tenant_a_id = next(t["id"] for t in tenants if t["clinic_name"] == CLINIC_A)

    resp = await client.delete(f"/admin/tenants/{tenant_a_id}", headers=_bearer(admin_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == tenant_a_id
    assert body["deleted"]["users"] == 1
    assert body["deleted"]["entitlements"] == 1
    assert body["deleted"]["refresh_tokens"] >= 1
    # SECRETARIA_* is unset in tests -> the cross-DB leg is skipped, not attempted.
    assert body["secretaria"]["status"] == "skipped_unconfigured"

    # Clinic A is gone from the listing and 404s on detail.
    remaining = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    assert all(t["clinic_name"] != CLINIC_A for t in remaining)
    assert (
        await client.get(f"/admin/tenants/{tenant_a_id}", headers=_bearer(admin_token))
    ).status_code == 404

    # Its owner is gone from the users view and can no longer authenticate.
    users = (await client.get("/admin/users", headers=_bearer(admin_token))).json()["items"]
    assert all(u["email"] != OWNER_A_EMAIL for u in users)
    assert (
        await client.post(
            "/auth/token", json={"email": OWNER_A_EMAIL, "password": OWNER_A_PASSWORD}
        )
    ).status_code == 401

    # Re-deleting the same tenant now 404s.
    assert (
        await client.delete(f"/admin/tenants/{tenant_a_id}", headers=_bearer(admin_token))
    ).status_code == 404


# --- Service (cascade internals + LGPD audit survival) ----------------------


async def test_delete_tenant_sweeps_children_but_keeps_audit(db_session):
    """delete_tenant removes every brain-owned child row; a privacy_requests audit row
    survives with requested_by nulled (its ON DELETE SET NULL semantics, applied here)."""
    tenant = Tenant(clinic_name="Cascade Clinic")
    db_session.add(tenant)
    await db_session.flush()

    owner = User(
        tenant_id=tenant.id,
        email="owner@cascade.com",
        name="Owner",
        password_hash="x",
        role=ROLE_TENANT_OWNER,
    )
    db_session.add(owner)
    await db_session.flush()

    db_session.add_all(
        [
            Entitlement(tenant_id=tenant.id, plan="precheck", status="active"),
            UsageEvent(id=f"evt:{uuid4()}", tenant_id=tenant.id, feature="patients", amount=1),
            RefreshToken(
                user_id=owner.id,
                token_hash=uuid4().hex,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            ),
            # An LGPD audit row attributed to this tenant's owner — must OUTLIVE the delete.
            PrivacyRequest(
                kind="erasure",
                subject_type="user",
                subject_hash=uuid4().hex,
                requested_by=owner.id,
                status="completed",
                result={},
            ),
        ]
    )
    await db_session.commit()

    result = await admin_service.delete_tenant(db_session, tenant.id)

    assert result.counts["users"] == 1
    assert result.counts["entitlements"] == 1
    assert result.counts["usage_events"] == 1
    assert result.counts["refresh_tokens"] == 1
    assert result.secretaria["status"] == "skipped_unconfigured"

    # Tenant + every scoped child are gone.
    assert await db_session.get(Tenant, tenant.id) is None
    for model in (User, Entitlement, UsageEvent, RefreshToken):
        assert (await db_session.scalar(select(func.count()).select_from(model))) == 0

    # The audit row remains, now detached from the deleted user.
    audit = (await db_session.scalars(select(PrivacyRequest))).all()
    assert len(audit) == 1
    assert audit[0].requested_by is None
