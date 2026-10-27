"""Tenant-locked team mutations; no platform-wide admin surface or shared passwords."""

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal
from brain_api.config import get_settings
from brain_api.core.security import hash_password, hash_refresh_token
from brain_api.models import RefreshToken, StaffAccount, StaffMembershipInvite, Tenant, User
from brain_api.schemas.team import TeamInviteInput, TeamInviteResult, TeamMember
from brain_api.services import mailer, onboarding_sync, secretaria_provisioning
from brain_api.services.staff_first_access import generate_temporary_password


def can_manage_team(user: User) -> bool:
    return bool(
        user.role == "manager" or (user.role == "doctor" and (user.is_owner or user.is_manager))
    )


def member_out(user: User) -> TeamMember:
    state = (
        "revoked"
        if user.access_revoked_at
        else (
            "pending_first_access"
            if user.requires_password_change or user.invite_token_hash or user.membership_pending
            else "active"
        )
    )
    return TeamMember(
        user_id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        is_manager=user.is_manager,
        is_owner=user.is_owner,
        professional_id=user.professional_id,
        access_status=state,
        delivery_status=user.invite_delivery_status,
        access_version=user.credential_version,
    )


def staff_login_url() -> str:
    settings = get_settings()
    url = settings.BRAIN_MESSAGE_PUBLIC_URL.strip()
    parsed = urlsplit(url)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != ""
        or parsed.scheme not in ("http", "https")
        or (settings.APP_ENV not in ("dev", "test") and parsed.scheme != "https")
    ):
        raise HTTPException(503, "staff_login_url_not_configured")
    return f"{url.rstrip('/')}/login"


async def lock_clinic(
    session: AsyncSession, principal: Principal, *, require_manager: bool = True
) -> Tenant:
    tenant = await session.scalar(
        select(Tenant).where(Tenant.id == principal.tenant_id).with_for_update()
    )
    if tenant is None:
        raise HTTPException(404, "Clinic not found")
    actor = await session.get(User, UUID(principal.user_id), populate_existing=True)
    if (
        not actor
        or actor.tenant_id != tenant.id
        or actor.access_revoked_at
        or actor.requires_password_change
        or actor.membership_pending
        or actor.credential_version != principal.credential_version
    ):
        raise HTTPException(401, "Invalid or expired token")
    if require_manager and not can_manage_team(actor):
        raise HTTPException(403, "team_manager_required")
    return tenant


async def target_member(session: AsyncSession, tenant_id: UUID, user_id: UUID) -> User:
    user = await session.scalar(
        select(User)
        .where(User.id == user_id, User.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise HTTPException(404, "Team member not found")
    return user


async def require_remaining_manager(session: AsyncSession, tenant_id: UUID, target: User) -> None:
    if target.is_owner:
        raise HTTPException(409, "clinic_owner_protected")
    if (
        not can_manage_team(target)
        or target.access_revoked_at
        or target.requires_password_change
        or target.membership_pending
    ):
        return
    other = await session.scalar(
        select(User.id)
        .outerjoin(StaffAccount, StaffAccount.id == User.account_id)
        .where(
            User.tenant_id == tenant_id,
            User.id != target.id,
            User.access_revoked_at.is_(None),
            User.requires_password_change.is_(False),
            User.membership_pending.is_(False),
            or_(
                User.account_id.is_(None),
                StaffAccount.disabled_at.is_(None)
                & StaffAccount.requires_password_change.is_(False),
            ),
            or_(
                User.is_owner.is_(True),
                User.role == "manager",
                (User.role == "doctor") & User.is_manager.is_(True),
            ),
        )
    )
    if other is None:
        raise HTTPException(409, "last_manager_protected")


async def deliver_first_access(
    session: AsyncSession, user: User, tenant: Tenant, password: str, url: str
) -> TeamInviteResult:
    version = user.credential_version
    try:
        sent = await mailer.send_email(
            user.email,
            "staff_temporary_password",
            {
                "name": user.name,
                "clinic_name": tenant.clinic_name,
                "email": user.email,
                "temporary_password": password,
                "login_url": url,
            },
        )
    except Exception:
        # A dependency failure must not expose request bodies or the generated password.
        sent = False
    # brain-api delivers directly since TASK-056: the outcome is known here, so the
    # status is final. "queued" remains a valid value only for rows written before.
    status = "sent" if sent else "failed"
    await session.execute(
        update(User)
        .where(
            User.id == user.id,
            User.credential_version == version,
            User.invite_delivery_status == "not_requested",
        )
        .values(invite_delivery_status=status)
    )
    await session.commit()
    await session.refresh(user)
    return TeamInviteResult(member=member_out(user), delivery_status=user.invite_delivery_status)


async def invite_team_member(
    session: AsyncSession, principal: Principal, payload: TeamInviteInput
) -> TeamInviteResult:
    url = staff_login_url()  # fail before remote or local creation
    tenant = await lock_clinic(session, principal)
    email = str(payload.email).lower()
    account = await session.scalar(select(StaffAccount).where(StaffAccount.email == email))
    if account and account.disabled_at:
        raise HTTPException(409, "email_already_registered")
    existing = list(await session.scalars(select(User).where(User.email == email)))
    if existing and not get_settings().STAFF_ACCOUNT_CREDENTIALS_ENABLED:
        raise HTTPException(409, "email_already_registered")
    if existing and account is None and len(existing) == 1:
        from brain_api.services.staff_accounts import adopt_user

        account = await adopt_user(session, existing[0])
    if await session.scalar(
        select(User.id).where(User.email == email, User.tenant_id == tenant.id)
    ):
        raise HTTPException(409, "email_already_registered")
    if existing and (not get_settings().STAFF_MULTI_CLINIC_ENABLED or account is None):
        raise HTTPException(409, "email_already_registered")
    professional_id = None
    if payload.role == "doctor":
        remote = await secretaria_provisioning.create_professional(
            tenant.id, name=payload.name, specialty=payload.specialty, about=None, interactive=True
        )
        if not remote or not remote.get("professional_id"):
            raise HTTPException(502, "secretaria_unavailable")
        professional_id = UUID(str(remote["professional_id"]))
        if await session.scalar(
            select(User.id).where(
                User.tenant_id == tenant.id, User.professional_id == professional_id
            )
        ):
            raise HTTPException(409, "professional_already_linked")
    if account:
        if not get_settings().STAFF_MULTI_CLINIC_ENABLED:
            raise HTTPException(409, "email_already_registered")
        from brain_api.services.staff_invitations import invite_existing

        try:
            return await invite_existing(session, account, tenant, payload, professional_id, url)
        except IntegrityError:
            await session.rollback()
            raise HTTPException(409, "email_already_registered") from None
    password = generate_temporary_password()
    user = User(
        tenant_id=tenant.id,
        name=payload.name,
        email=email,
        role=payload.role,
        is_manager=payload.is_manager,
        professional_id=professional_id,
        password_hash=hash_password(password),
        requires_password_change=True,
        temporary_password_expires_at=datetime.now(UTC) + timedelta(hours=72),
    )
    session.add(user)
    try:
        await session.flush()
        from brain_api.services.staff_accounts import adopt_new_user

        await adopt_new_user(session, user)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "email_already_registered") from None
    await session.refresh(user)
    onboarding_sync.invalidate_config_status(tenant.id)
    return await deliver_first_access(session, user, tenant, password, url)


async def set_member_management(
    session: AsyncSession, principal: Principal, user_id: UUID, enabled: bool
) -> TeamMember:
    tenant = await lock_clinic(session, principal)
    user = await target_member(session, tenant.id, user_id)
    if user.role != "doctor":
        raise HTTPException(422, "only_doctor_management_can_change")
    if user.is_owner:
        raise HTTPException(409, "clinic_owner_protected")
    if user.access_revoked_at:
        raise HTTPException(409, "access_revoked")
    if user.is_manager != enabled:
        if not enabled:
            await require_remaining_manager(session, tenant.id, user)
        user.is_manager = enabled
        user.credential_version += 1
        if user.membership_pending:
            user.invite_delivery_status = "not_requested"
        await session.commit()
    return member_out(user)


async def resend_member_first_access(
    session: AsyncSession, principal: Principal, user_id: UUID
) -> TeamInviteResult:
    url = staff_login_url()
    before = await session.scalar(
        select(User).where(User.id == user_id, User.tenant_id == principal.tenant_id)
    )
    locked_account_id = before.account_id if before else None
    if locked_account_id:
        await session.scalar(
            select(StaffAccount)
            .where(StaffAccount.id == locked_account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    tenant = await lock_clinic(session, principal)
    user = await target_member(session, tenant.id, user_id)
    if user.account_id and user.account_id != locked_account_id:
        # Adoption crossed the initial read; restart before writing with account -> user locks.
        await session.commit()
        return await resend_member_first_access(session, principal, user_id)
    if user.access_revoked_at:
        raise HTTPException(409, "first_access_not_pending")
    if user.membership_pending:
        from brain_api.services.staff_invitations import resend_invitation

        return await resend_invitation(session, user, tenant, url)
    if user.access_revoked_at or (not user.requires_password_change and not user.invite_token_hash):
        raise HTTPException(409, "first_access_not_pending")
    password = generate_temporary_password()
    if user.account_id:
        account = await session.scalar(
            select(StaffAccount).where(StaffAccount.id == user.account_id).with_for_update()
        )
        if not account or not account.requires_password_change:
            raise HTTPException(409, "first_access_not_pending")
        account.password_hash = hash_password(password)
        account.temporary_password_expires_at = datetime.now(UTC) + timedelta(hours=72)
        account.credential_version += 1
    user.password_hash = hash_password(password)
    user.requires_password_change = True
    user.temporary_password_expires_at = datetime.now(UTC) + timedelta(hours=72)
    user.credential_version += 1
    user.invite_token_hash = None
    user.invite_token_expires_at = None
    user.invite_delivery_status = "not_requested"
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()
    return await deliver_first_access(session, user, tenant, password, url)


async def revoke_member_access(
    session: AsyncSession, principal: Principal, user_id: UUID
) -> TeamMember:
    tenant = await lock_clinic(session, principal)
    user = await target_member(session, tenant.id, user_id)
    await require_remaining_manager(session, tenant.id, user)
    if user.access_revoked_at is None:
        # An unconsumed legacy invite is an obligation, not an active membership.
        if user.invite_token_hash:
            user.requires_password_change = True
        user.access_revoked_at = datetime.now(UTC)
        user.credential_version += 1
        user.invite_token_hash = None
        user.invite_token_expires_at = None
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await session.commit()
    return member_out(user)


async def restore_member_access(
    session: AsyncSession, principal: Principal, user_id: UUID, expected_version: int
) -> TeamMember:
    # Match setup/reset/invitation lock order: account before clinic and membership.
    before = await session.scalar(
        select(User).where(User.id == user_id, User.tenant_id == principal.tenant_id)
    )
    account_id = before.account_id if before else None
    account = None
    if account_id:
        account = await session.scalar(
            select(StaffAccount)
            .where(StaffAccount.id == account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    tenant = await lock_clinic(session, principal)
    user = await target_member(session, tenant.id, user_id)
    if user.account_id != account_id:
        await session.commit()
        return await restore_member_access(session, principal, user_id, expected_version)
    if user.is_owner:
        raise HTTPException(409, "clinic_owner_protected")
    if user.access_revoked_at is None:
        # Retrying the same successful transition must not remove a later grant.
        if user.credential_version not in (expected_version, expected_version + 1):
            raise HTTPException(409, "team_member_changed")
        return member_out(user)
    if user.credential_version != expected_version:
        raise HTTPException(409, "team_member_changed")
    if account_id and (not account or account.disabled_at):
        raise HTTPException(409, "account_unavailable")
    if user.requires_password_change and account and not account.requires_password_change:
        # Never replace a personal password that has already been established.
        raise HTTPException(409, "first_access_not_pending")
    values = {
        "access_revoked_at": None,
        "credential_version": User.credential_version + 1,
        "invite_token_hash": None,
        "invite_token_expires_at": None,
    }
    if user.role == "doctor":
        values["is_manager"] = False
    password = None
    invitation = None
    raw_invitation = None
    url = None
    if user.requires_password_change:
        # Owner decision: restoration also resends the first-access e-mail. Only
        # an unfinished personal credential may get a new temporary password.
        url = staff_login_url()
        password = generate_temporary_password()
        password_hash = hash_password(password)
        expires_at = datetime.now(UTC) + timedelta(hours=72)
        values["password_hash"] = password_hash
        values["temporary_password_expires_at"] = expires_at
        values["invite_delivery_status"] = "not_requested"
    elif user.membership_pending:
        url = staff_login_url()
        invitation = await session.scalar(
            select(StaffMembershipInvite)
            .where(
                StaffMembershipInvite.user_id == user.id,
                StaffMembershipInvite.accepted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if invitation is None:
            raise HTTPException(409, "first_access_not_pending")
        raw_invitation = secrets.token_urlsafe(32)
        values["invite_delivery_status"] = "not_requested"
    changed = await session.execute(
        update(User)
        .where(
            User.id == user.id,
            User.tenant_id == tenant.id,
            User.access_revoked_at.is_not(None),
            User.credential_version == expected_version,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise HTTPException(409, "team_member_changed")
    if password and account:
        account.password_hash = password_hash
        account.temporary_password_expires_at = expires_at
        account.credential_version += 1
    if invitation:
        invitation.membership_version = expected_version + 1
        invitation.token_hash = hash_refresh_token(raw_invitation)
        invitation.expires_at = datetime.now(UTC) + timedelta(hours=72)
        invitation.is_manager = False if user.role == "doctor" else user.is_manager
        invitation.delivery_status = "not_requested"
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()
    await session.refresh(user)
    if password:
        return (await deliver_first_access(session, user, tenant, password, url)).member
    if invitation:
        from brain_api.services.staff_invitations import deliver_invitation

        return (
            await deliver_invitation(session, user, invitation, tenant, raw_invitation, url)
        ).member
    return member_out(user)


async def active_staff_roles(session: AsyncSession, tenant_id: UUID) -> list[str]:
    """Distinct `role`s of the tenant's members who can act now, sorted; never a person.

    "Active" = the team screen's own state (`member_out` == "active": not revoked, no
    pending first access, invite or membership) AND, for a linked personal account, the
    account rule `require_remaining_manager` already applies (not disabled, not waiting
    for its first password). Read live, every call (TASK-052 Task 16: secretarIA asks
    whether the clinic has a working secretary; nothing is cached on either side).
    """
    rows = await session.scalars(
        select(User.role)
        .distinct()
        .outerjoin(StaffAccount, StaffAccount.id == User.account_id)
        .where(
            User.tenant_id == tenant_id,
            User.access_revoked_at.is_(None),
            User.requires_password_change.is_(False),
            User.membership_pending.is_(False),
            User.invite_token_hash.is_(None),
            or_(
                User.account_id.is_(None),
                StaffAccount.disabled_at.is_(None)
                & StaffAccount.requires_password_change.is_(False),
            ),
        )
    )
    return sorted({role for role in rows.all() if role})
