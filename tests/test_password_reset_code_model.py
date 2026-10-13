"""staff_password_reset_codes: one row per user, removed with the user (TASK-056)."""

from datetime import UTC, datetime, timedelta

from brain_api.core.security import hash_password
from brain_api.models import StaffPasswordResetCode, Tenant, User


async def test_one_code_row_per_user(db_session):
    tenant = Tenant(clinic_name="Clínica")
    db_session.add(tenant)
    await db_session.flush()
    user = User(
        tenant_id=tenant.id,
        email="ana@example.com",
        name="Ana",
        password_hash=hash_password("Senha123"),
        role="doctor",
    )
    db_session.add(user)
    await db_session.flush()
    now = datetime.now(UTC)
    db_session.add(
        StaffPasswordResetCode(
            user_id=user.id,
            code_hash="x" * 64,
            expires_at=now + timedelta(minutes=10),
            window_started_at=now,
            issued_in_window=1,
        )
    )
    await db_session.commit()
    row = await db_session.get(StaffPasswordResetCode, user.id)
    assert row.attempts == 0


def test_code_row_dies_with_the_user():
    (fk,) = StaffPasswordResetCode.__table__.c.user_id.foreign_keys
    assert fk.column.table.name == "users"
    assert fk.ondelete == "CASCADE"
