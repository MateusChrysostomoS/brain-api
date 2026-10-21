"""Platform support cannot bypass setup/revocation or mint an obsolete credential."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from brain_api.core.security import decode_token
from brain_api.models import Tenant, User
from brain_api.services.admin import issue_impersonation_token


async def test_impersonation_uses_current_credential_version(db_session):
    tenant = Tenant(clinic_name="Fixture")
    db_session.add(tenant)
    await db_session.flush()
    user = User(
        tenant_id=tenant.id,
        email="support-target@example.com",
        name="Fixture Doctor",
        role="doctor",
        credential_version=3,
        password_hash="not-a-live-password",
    )
    db_session.add(user)
    await db_session.commit()
    minted = await issue_impersonation_token(db_session, user.email)
    assert decode_token(minted.out.access_token)["credential_version"] == 3


@pytest.mark.parametrize("state", ["pending", "revoked"])
async def test_impersonation_cannot_bypass_access_state(db_session, state):
    tenant = Tenant(clinic_name="Fixture")
    db_session.add(tenant)
    await db_session.flush()
    user = User(
        tenant_id=tenant.id,
        email="support-target@example.com",
        name="Fixture Doctor",
        role="doctor",
        password_hash="not-a-live-password",
        requires_password_change=state == "pending",
        access_revoked_at=datetime.now(UTC) if state == "revoked" else None,
    )
    db_session.add(user)
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await issue_impersonation_token(db_session, user.email)
    assert error.value.status_code == 404
