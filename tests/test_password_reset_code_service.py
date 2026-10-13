"""services/auth.py — issuing and verifying e-mailed reset codes (TASK-056)."""

from datetime import UTC, datetime, timedelta

import pytest_asyncio

from brain_api.config import get_settings
from brain_api.core.security import hash_password, hash_refresh_token
from brain_api.models import StaffPasswordResetCode, Tenant, User
from brain_api.services.auth import (
    complete_password_reset,
    issue_password_reset_code,
    verify_password_reset_code,
)

EMAIL = "ana@example.com"


@pytest_asyncio.fixture
async def user(db_session):
    tenant = Tenant(clinic_name="Clínica")
    db_session.add(tenant)
    await db_session.flush()
    u = User(
        tenant_id=tenant.id,
        email=EMAIL,
        name="Ana",
        password_hash=hash_password("Senha123"),
        role="doctor",
    )
    db_session.add(u)
    await db_session.commit()
    return u


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


async def test_issue_returns_six_digit_code_and_stores_only_a_keyed_hash(db_session, user):
    issued = await issue_password_reset_code(db_session, EMAIL)
    assert issued is not None
    got_user, code = issued
    assert got_user.id == user.id
    assert len(code) == 6 and code.isdigit()
    row = await db_session.get(StaffPasswordResetCode, user.id)
    assert row.code_hash != hash_refresh_token(code)  # keyed HMAC, not bare sha256


async def test_unknown_and_revoked_get_none(db_session, user):
    assert await issue_password_reset_code(db_session, "nobody@example.com") is None
    user.access_revoked_at = datetime.now(UTC)
    await db_session.commit()
    assert await issue_password_reset_code(db_session, EMAIL) is None


async def test_correct_code_returns_token_once_and_burns_the_code(db_session, user):
    _, code = await issue_password_reset_code(db_session, EMAIL)
    token = await verify_password_reset_code(db_session, EMAIL, code)
    assert token
    assert await db_session.get(StaffPasswordResetCode, user.id) is None
    assert await verify_password_reset_code(db_session, EMAIL, code) is None
    assert await complete_password_reset(db_session, token, "NovaSenha9") is not None
    assert await complete_password_reset(db_session, token, "OutraSenha9") is None


async def test_verify_code_normalizes_email(db_session, user):
    _, code = await issue_password_reset_code(db_session, "  ANA@Example.com ")
    assert await verify_password_reset_code(db_session, " Ana@EXAMPLE.com", code)


async def test_five_wrong_attempts_kill_the_code(db_session, user):
    _, code = await issue_password_reset_code(db_session, EMAIL)
    for _ in range(get_settings().PASSWORD_RESET_CODE_MAX_ATTEMPTS):
        assert await verify_password_reset_code(db_session, EMAIL, _wrong(code)) is None
    assert await verify_password_reset_code(db_session, EMAIL, code) is None


async def test_expired_code_is_rejected(db_session, user):
    _, code = await issue_password_reset_code(db_session, EMAIL)
    row = await db_session.get(StaffPasswordResetCode, user.id)
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    assert await verify_password_reset_code(db_session, EMAIL, code) is None


async def test_reissue_invalidates_previous_code(db_session, user):
    _, first = await issue_password_reset_code(db_session, EMAIL)
    _, second = await issue_password_reset_code(db_session, EMAIL)
    if first != second:  # 1-in-10^6 chance they collide
        assert await verify_password_reset_code(db_session, EMAIL, first) is None
    assert await verify_password_reset_code(db_session, EMAIL, second)


async def test_hourly_issue_cap(db_session, user):
    cap = get_settings().PASSWORD_RESET_CODE_MAX_PER_HOUR
    for _ in range(cap):
        assert await issue_password_reset_code(db_session, EMAIL) is not None
    assert await issue_password_reset_code(db_session, EMAIL) is None
    row = await db_session.get(StaffPasswordResetCode, user.id)
    row.window_started_at = datetime.now(UTC) - timedelta(hours=1, seconds=1)
    await db_session.commit()
    assert await issue_password_reset_code(db_session, EMAIL) is not None


async def test_reset_token_lifetime_is_fifteen_minutes(db_session, user):
    _, code = await issue_password_reset_code(db_session, EMAIL)
    before = datetime.now(UTC)
    await verify_password_reset_code(db_session, EMAIL, code)
    await db_session.refresh(user)
    expires = user.reset_token_expires_at
    expires = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
    assert timedelta(minutes=14) < expires - before <= timedelta(minutes=15, seconds=5)
