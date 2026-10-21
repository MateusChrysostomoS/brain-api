"""Isolated clinical roles, revocation, remembered selection and proof expiry."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from brain_api.api.deps import get_current_principal, get_token_principal, require_team_manager
from brain_api.api.staff_account import response_body
from brain_api.config import get_settings
from brain_api.core.security import hash_password
from brain_api.models import StaffAccount, StaffAccountResetCode, Tenant, User
from brain_api.services import staff_accounts as service
from brain_api.services.auth import (
    complete_password_reset,
    issue_password_reset_code,
    verify_password_reset_code,
)
from tests.test_rbac import OWNER_A_EMAIL, OWNER_A_PASSWORD

pytestmark = pytest.mark.usefixtures("staff_accounts_enabled")


async def seed(session):
    a, b = Tenant(clinic_name="A"), Tenant(clinic_name="B")
    account = StaffAccount(
        email="person@example.com", name="Person", password_hash=hash_password("Password123")
    )
    session.add_all([a, b, account])
    await session.flush()
    ua = User(
        account_id=account.id,
        tenant_id=a.id,
        email=account.email,
        name="Person",
        password_hash="!account-managed",
        role="manager",
        is_manager=True,
    )
    ub = User(
        account_id=account.id,
        tenant_id=b.id,
        email=account.email,
        name="Person",
        password_hash="!account-managed",
        role="doctor",
        is_manager=False,
    )
    session.add_all([ua, ub])
    await session.commit()
    row, _ = await service.open_account_session(session, account)
    return account, ua, ub, row


async def test_role_and_revocation_are_local_and_preference_never_falls_back(db_session):
    account, a, b, row = await seed(db_session)
    await service.remember_membership(db_session, account, a.id)
    body = await response_body(db_session, account, row)
    assert body.clinical_session.role == "manager"
    a.access_revoked_at = datetime.now(UTC)
    a.credential_version += 1
    await db_session.commit()
    body = await response_body(db_session, account, row)
    assert body.selection_state == "preferred_unavailable"
    assert body.clinical_session is None
    user = await service.select_membership(db_session, account, b.id)
    from brain_api.api.auth import build_session_response

    token = build_session_response(user, None, account_session_id=str(row.id)).access_token
    principal = await get_current_principal(get_token_principal(f"Bearer {token}"), db_session)
    assert principal.role == "doctor" and not principal.is_manager
    with pytest.raises(HTTPException):
        require_team_manager(principal)
    await service.remember_membership(db_session, account, None)
    assert (await response_body(db_session, account, row)).selection_state == "selection_required"


async def test_reset_invalidates_both_contexts_without_restoring_access(db_session):
    account, a, b, row = await seed(db_session)
    from brain_api.api.auth import build_session_response

    tokens = [
        build_session_response(user, None, account_session_id=str(row.id)).access_token
        for user in (a, b)
    ]
    a.access_revoked_at = datetime.now(UTC)
    await db_session.commit()
    _, code = await issue_password_reset_code(db_session, account.email)
    token = await verify_password_reset_code(db_session, account.email, code)
    assert await complete_password_reset(db_session, token, "NewPassword123")
    for old in tokens:
        with pytest.raises(HTTPException):
            await get_current_principal(get_token_principal(f"Bearer {old}"), db_session)
    assert await service.authenticate_account(db_session, account.email, "Password123") is None
    assert await service.authenticate_account(db_session, account.email, "NewPassword123")
    await db_session.refresh(a)
    assert a.access_revoked_at
    assert (await service.select_membership(db_session, account, b.id)).id == b.id


async def test_recovery_attempts_reissue_expiry_replay_and_quota(db_session):
    account, _, _, _ = await seed(db_session)
    _, old = await issue_password_reset_code(db_session, account.email)
    _, current = await issue_password_reset_code(db_session, account.email)
    assert await verify_password_reset_code(db_session, account.email, old) is None
    token = await verify_password_reset_code(db_session, account.email, current)
    assert token
    assert await verify_password_reset_code(db_session, account.email, current) is None
    row = await db_session.get(StaffAccountResetCode, account.id)
    assert row.issued_in_window == 2
    for _ in range(get_settings().PASSWORD_RESET_CODE_MAX_PER_HOUR - 2):
        assert await issue_password_reset_code(db_session, account.email)
    assert await issue_password_reset_code(db_session, account.email) is None
    row = await db_session.get(StaffAccountResetCode, account.id)
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    assert await verify_password_reset_code(db_session, account.email, current) is None


async def test_account_cookie_csrf_rotation_and_replay(client, monkeypatch):
    client.base_url = "https://test"
    result = await client.post(
        "/auth/account/login",
        json={
            "email": OWNER_A_EMAIL,
            "password": OWNER_A_PASSWORD,
        },
    )
    original = client.cookies.get("__Host-staff_account")
    assert original
    assert (await client.post("/auth/account/resume")).status_code == 403
    resumed = await client.post("/auth/account/resume", headers={"X-Brain-Client": "web"})
    assert resumed.status_code == 200
    assert client.cookies.get("__Host-staff_account") != original
    # TASK-065: the session row is renewed in place, so a token bound to it before the
    # resume (another tab's) stays valid until its own short expiry.
    old_proof = {"Authorization": f"Bearer {result.json()['account_token']}"}
    assert (await client.get("/auth/account/memberships", headers=old_proof)).status_code == 200
    replay_headers = {
        "X-Brain-Client": "web",
        "Cookie": f"__Host-staff_account={original}",
    }
    # Inside the grace window the replaced value is a renewal in flight: accepted, no cookie.
    in_flight = await client.post("/auth/account/resume", headers=replay_headers)
    assert in_flight.status_code == 200
    assert "set-cookie" not in in_flight.headers
    # After it, the same value is the reuse signal and ends the whole account.
    monkeypatch.setattr(get_settings(), "STAFF_ACCOUNT_SESSION_ROTATION_GRACE_SECONDS", 0)
    replay = await client.post("/auth/account/resume", headers=replay_headers)
    assert replay.status_code == 401
    assert "Max-Age=0" in replay.headers["set-cookie"]
    proof = {"Authorization": f"Bearer {resumed.json()['account_token']}"}
    assert (await client.get("/auth/account/memberships", headers=proof)).status_code == 401
    assert (await client.get("/auth/account/memberships", headers=old_proof)).status_code == 401


async def test_refresh_cannot_be_issued_from_password_proof_before_reset(db_session):
    account, a, _, _ = await seed(db_session)
    proof_version = a.credential_version
    _, code = await issue_password_reset_code(db_session, account.email)
    reset = await verify_password_reset_code(db_session, account.email, code)
    await complete_password_reset(db_session, reset, "NewPassword123")
    from brain_api.services.auth import issue_refresh_token

    with pytest.raises(HTTPException):
        await issue_refresh_token(db_session, a.id, expected_version=proof_version)


async def test_account_token_is_not_valid_for_shared_key_consumers(db_session):
    account, _, _, row = await seed(db_session)
    from brain_api.core.security import decode_token

    proof = service.account_token(account, row)
    accepted_by_shared_key = decode_token(proof) is not None
    assert not accepted_by_shared_key
    assert (await service.resolve_account(db_session, proof))[0].id == account.id


async def test_new_setup_proof_is_not_valid_for_shared_key_consumers(db_session):
    _, user, _, _ = await seed(db_session)
    from brain_api.core.security import decode_token
    from brain_api.services.staff_first_access import create_staff_setup_token

    user.temporary_password_expires_at = datetime.now(UTC) + timedelta(hours=72)
    proof = create_staff_setup_token(user)
    accepted_by_shared_key = decode_token(proof) is not None
    assert not accepted_by_shared_key
