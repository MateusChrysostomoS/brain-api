"""Target-managed invitations. Proof of the personal account is required to accept."""

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.core.security import hash_refresh_token
from brain_api.models import StaffAccount, StaffMembershipInvite, Tenant, User
from brain_api.schemas.team import TeamInviteInput, TeamInviteResult
from brain_api.services import mailer


async def invite_existing(
    session: AsyncSession,
    account: StaffAccount,
    tenant: Tenant,
    payload: TeamInviteInput,
    professional_id,
    url: str,
) -> TeamInviteResult:
    if account.disabled_at:
        raise HTTPException(409, "email_already_registered")
    user = User(
        account_id=account.id,
        tenant_id=tenant.id,
        email=account.email,
        name=payload.name,
        role=payload.role,
        is_manager=payload.is_manager,
        is_owner=False,
        professional_id=professional_id,
        password_hash="!account-managed",
        membership_pending=True,
    )
    session.add(user)
    await session.flush()
    raw = secrets.token_urlsafe(32)
    invitation = StaffMembershipInvite(
        account_id=account.id,
        tenant_id=tenant.id,
        user_id=user.id,
        membership_version=user.credential_version,
        name=user.name,
        role=user.role,
        is_manager=user.is_manager,
        professional_id=user.professional_id,
        token_hash=hash_refresh_token(raw),
        expires_at=datetime.now(UTC) + timedelta(hours=72),
    )
    session.add(invitation)
    await session.commit()
    return await deliver_invitation(session, user, invitation, tenant, raw, url)


async def deliver_invitation(session, user, invitation, tenant, raw, url):
    sent = await mailer.send_email(
        user.email,
        "staff_membership_invite",
        {
            "name": user.name,
            "clinic_name": tenant.clinic_name,
            "link": f"{url}?invite={raw}",
        },
    )
    delivery = "sent" if sent else "failed"
    await session.execute(
        update(User)
        .where(
            User.id == user.id,
            User.credential_version == invitation.membership_version,
            User.membership_pending.is_(True),
        )
        .values(invite_delivery_status=delivery)
    )
    await session.execute(
        update(StaffMembershipInvite)
        .where(
            StaffMembershipInvite.id == invitation.id,
            StaffMembershipInvite.token_hash == hash_refresh_token(raw),
        )
        .values(delivery_status=delivery)
    )
    await session.commit()
    await session.refresh(user)
    from brain_api.services.team import member_out

    return TeamInviteResult(member=member_out(user), delivery_status=user.invite_delivery_status)


async def resend_invitation(session, user, tenant, url):
    invitation = await session.scalar(
        select(StaffMembershipInvite)
        .where(
            StaffMembershipInvite.user_id == user.id,
            StaffMembershipInvite.accepted_at.is_(None),
        )
        .with_for_update()
    )
    if not invitation or user.access_revoked_at:
        raise HTTPException(409, "first_access_not_pending")
    raw = secrets.token_urlsafe(32)
    user.credential_version += 1
    invitation.membership_version = user.credential_version
    invitation.token_hash = hash_refresh_token(raw)
    invitation.expires_at = datetime.now(UTC) + timedelta(hours=72)
    invitation.delivery_status = "not_requested"
    user.invite_delivery_status = "not_requested"
    await session.commit()
    return await deliver_invitation(session, user, invitation, tenant, raw, url)


async def accept_invitation(
    session: AsyncSession, account: StaffAccount, token: str, *, session_id=None
):
    from brain_api.services.staff_accounts import aware

    # Lock the account first: password reset/disable and acceptance serialize.
    account = await session.scalar(
        select(StaffAccount)
        .where(StaffAccount.id == account.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account.disabled_at or account.requires_password_change:
        raise HTTPException(401, "Invalid or expired token")
    if session_id:
        from brain_api.services.staff_accounts import live_session

        current_account, _ = await live_session(session, session_id)
        if current_account.id != account.id:
            raise HTTPException(401, "Invalid or expired token")
    invitation = await session.scalar(
        select(StaffMembershipInvite)
        .where(
            StaffMembershipInvite.account_id == account.id,
            StaffMembershipInvite.token_hash == hash_refresh_token(token),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = datetime.now(UTC)
    if not invitation or invitation.accepted_at or aware(invitation.expires_at) <= now:
        raise HTTPException(404, "membership_invite_not_found")
    target = await session.scalar(
        select(User)
        .where(
            User.id == invitation.user_id,
            User.account_id == account.id,
            User.tenant_id == invitation.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not target
        or not target.membership_pending
        or target.access_revoked_at
        or target.credential_version != invitation.membership_version
    ):
        raise HTTPException(404, "membership_invite_not_found")
    consumed = await session.execute(
        update(StaffMembershipInvite)
        .where(
            StaffMembershipInvite.id == invitation.id,
            StaffMembershipInvite.accepted_at.is_(None),
            StaffMembershipInvite.token_hash == hash_refresh_token(token),
        )
        .values(accepted_at=now)
        .execution_options(synchronize_session=False)
    )
    if consumed.rowcount != 1:
        raise HTTPException(404, "membership_invite_not_found")
    changed = await session.execute(
        update(User)
        .where(
            User.id == target.id,
            User.membership_pending.is_(True),
            User.access_revoked_at.is_(None),
            User.credential_version == invitation.membership_version,
        )
        .values(membership_pending=False, credential_version=User.credential_version + 1)
    )
    if changed.rowcount != 1:
        raise HTTPException(404, "membership_invite_not_found")
    await session.commit()
    await session.refresh(invitation)
    await session.refresh(target)
    return target
