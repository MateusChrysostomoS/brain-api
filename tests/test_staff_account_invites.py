"""A target clinic's invitation is the only way to add another membership."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import HTTPException

from brain_api.api.deps import Principal
from brain_api.config import get_settings
from brain_api.core.security import hash_password, verify_password
from brain_api.models import StaffMembershipInvite, Tenant, User
from brain_api.schemas.team import TeamInviteInput
from brain_api.services import staff_accounts, team

pytestmark = pytest.mark.usefixtures("staff_accounts_enabled")


async def test_known_account_invite_never_changes_password(db_session, monkeypatch, mail_spy):
    monkeypatch.setattr(get_settings(), "STAFF_MULTI_CLINIC_ENABLED", True)
    monkeypatch.setattr(get_settings(), "BRAIN_MESSAGE_PUBLIC_URL", "https://portal.example.test")
    a, b = Tenant(clinic_name="A"), Tenant(clinic_name="B")
    db_session.add_all([a, b])
    await db_session.flush()
    original = User(
        tenant_id=a.id,
        email="person@example.com",
        name="Person",
        role="doctor",
        password_hash=hash_password("Password123"),
        is_manager=True,
    )
    db_session.add(original)
    await db_session.flush()
    account = await staff_accounts.adopt_user(db_session, original)
    await db_session.commit()
    db_session.add(
        User(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            tenant_id=b.id,
            email="manager@example.com",
            name="Manager",
            role="manager",
            is_manager=True,
            password_hash="test-only-not-for-login",
        )
    )
    await db_session.commit()
    manager = Principal("00000000-0000-0000-0000-000000000001", b.id, "manager", is_manager=True)
    invitation = await team.invite_team_member(
        db_session,
        manager,
        TeamInviteInput(
            name="Person",
            email="person@example.com",
            role="manager",
        ),
    )
    assert invitation.member.user_id != original.id
    target = await db_session.get(User, invitation.member.user_id)
    assert target.membership_pending
    assert verify_password("Password123", account.password_hash)
    assert target.password_hash == "!account-managed"
    with pytest.raises(HTTPException) as pending:
        await staff_accounts.select_membership(db_session, account, target.id)
    assert pending.value.detail == "membership_pending"
    from sqlalchemy import select

    invite = await db_session.scalar(select(StaffMembershipInvite))
    # The real outgoing captured message is the source of the one-use proof.
    captured = mail_spy.calls[-1]
    token = captured[2]["link"].split("invite=")[1]
    await staff_accounts.accept_invite(db_session, account, token)
    assert (
        await staff_accounts.select_membership(db_session, account, target.id)
    ).role == "manager"
    assert original.role == "doctor"
    assert original.is_manager
    assert invite.accepted_at is not None
    with pytest.raises(HTTPException):
        await staff_accounts.accept_invite(db_session, account, token)


async def test_revocation_blocks_old_invite(db_session, monkeypatch, mail_spy):
    # Additional race coverage runs against distinct PostgreSQL connections.
    monkeypatch.setattr(get_settings(), "STAFF_MULTI_CLINIC_ENABLED", True)
    monkeypatch.setattr(get_settings(), "BRAIN_MESSAGE_PUBLIC_URL", "https://portal.example.test")
    a, b = Tenant(clinic_name="A"), Tenant(clinic_name="B")
    db_session.add_all([a, b])
    await db_session.flush()
    user = User(
        tenant_id=a.id,
        email="person@example.com",
        name="Person",
        role="doctor",
        password_hash=hash_password("Password123"),
    )
    db_session.add(user)
    await db_session.flush()
    account = await staff_accounts.adopt_user(db_session, user)
    await db_session.commit()
    db_session.add(
        User(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            tenant_id=b.id,
            email="manager@example.com",
            name="Manager",
            role="manager",
            is_manager=True,
            password_hash="test-only-not-for-login",
        )
    )
    await db_session.commit()
    manager = Principal("00000000-0000-0000-0000-000000000001", b.id, "manager", is_manager=True)
    invitation = await team.invite_team_member(
        db_session,
        manager,
        TeamInviteInput(
            name="Person",
            email=user.email,
            role="secretary",
        ),
    )
    target = await db_session.get(User, invitation.member.user_id)
    token = mail_spy.calls[-1][2]["link"].split("invite=")[1]
    target.access_revoked_at = datetime.now(UTC)
    target.credential_version += 1
    await db_session.commit()
    with pytest.raises(HTTPException):
        await staff_accounts.accept_invite(db_session, account, token)
