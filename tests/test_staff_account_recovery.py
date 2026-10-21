"""Recovery belongs to a person, including when every clinic revoked the portal."""

from datetime import UTC, datetime

from brain_api.core.security import hash_password, verify_password
from brain_api.models import StaffAccount, Tenant, User
from brain_api.services.auth import (
    complete_password_reset,
    issue_password_reset_code,
    verify_password_reset_code,
)
from brain_api.services.staff_accounts import adopt_user


async def test_account_reset_keeps_revocation_and_common_password(db_session):
    clinic = Tenant(clinic_name="A")
    db_session.add(clinic)
    await db_session.flush()
    user = User(
        tenant_id=clinic.id,
        email="person@example.test",
        name="Person",
        role="doctor",
        password_hash=hash_password("Password123"),
        access_revoked_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.flush()
    account = await adopt_user(db_session, user)
    await db_session.commit()
    issued = await issue_password_reset_code(db_session, user.email)
    assert issued is not None
    token = await verify_password_reset_code(db_session, user.email, issued[1])
    assert token
    assert await complete_password_reset(db_session, token, "NewPassword123") is not None
    await db_session.refresh(account)
    await db_session.refresh(user)
    assert verify_password("NewPassword123", account.password_hash)
    assert not verify_password("Password123", account.password_hash)
    assert user.access_revoked_at is not None
    assert not user.is_manager
    assert await complete_password_reset(db_session, token, "AgainPassword123") is None
    assert await db_session.get(StaffAccount, account.id)
