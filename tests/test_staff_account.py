"""Staff identity proof never grants another clinic's permissions."""

from datetime import UTC, datetime

from sqlalchemy import select

from brain_api.core.security import hash_password
from brain_api.models import Tenant, User
from brain_api.services import staff_accounts


async def test_revoked_membership_still_proves_person(db_session):
    clinic = Tenant(clinic_name="Clinic A")
    db_session.add(clinic)
    await db_session.flush()
    user = User(
        tenant_id=clinic.id,
        email="person@example.test",
        name="Person",
        password_hash=hash_password("Password123"),
        role="doctor",
        access_revoked_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.commit()
    account = await staff_accounts.authenticate_account(
        db_session, " PERSON@example.test ", "Password123"
    )
    assert account is not None
    memberships = await staff_accounts.list_memberships(db_session, account)
    assert len(memberships) == 1
    assert memberships[0].status == "revoked"
    assert memberships[0].user_id == user.id
    assert (
        await staff_accounts.authenticate_account(db_session, "person@example.test", "wrong")
        is None
    )
    assert (await db_session.scalar(select(User))).id == user.id
