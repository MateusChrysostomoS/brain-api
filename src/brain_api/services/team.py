"""Tenant-locked team mutations; no platform-wide admin surface or shared passwords."""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal
from brain_api.config import get_settings
from brain_api.core.security import hash_password
from brain_api.models import RefreshToken, Tenant, User
from brain_api.schemas.team import TeamInviteInput, TeamInviteResult, TeamMember
from brain_api.services import onboarding_sync, secretaria_provisioning
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
            if user.requires_password_change or user.invite_token_hash
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


async def lock_clinic(session: AsyncSession, principal: Principal) -> Tenant:
    tenant = await session.scalar(
        select(Tenant).where(Tenant.id == principal.tenant_id).with_for_update()
    )
    if tenant is None:
        raise HTTPException(404, "Clinic not found")
    return tenant


async def target_member(session: AsyncSession, tenant_id: UUID, user_id: UUID) -> User:
    user = await session.scalar(
        select(User).where(User.id == user_id, User.tenant_id == tenant_id).with_for_update()
    )
    if user is None:
        raise HTTPException(404, "Team member not found")
    return user


async def require_remaining_manager(session: AsyncSession, tenant_id: UUID, target: User) -> None:
    if target.is_owner:
        raise HTTPException(409, "clinic_owner_protected")
    if not can_manage_team(target) or target.access_revoked_at or target.requires_password_change:
        return
    other = await session.scalar(
        select(User.id).where(
            User.tenant_id == tenant_id,
            User.id != target.id,
            User.access_revoked_at.is_(None),
            User.requires_password_change.is_(False),
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
        queued = await secretaria_provisioning.send_notification_email(
            user.email,
            "staff_temporary_password",
            {
                "name": user.name,
                "clinic_name": tenant.clinic_name,
                "temporary_password": password,
                "login_url": url,
                "tenant_id": str(tenant.id),
                "user_id": str(user.id),
                "credential_version": version,
                "expires_at": user.temporary_password_expires_at.isoformat(),
            },
        )
    except Exception:
        # A dependency failure must not expose request bodies or the generated password.
        queued = False
    status = "queued" if queued else "failed"
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
    if await session.scalar(select(User.id).where(User.email == email)):
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
        # A pending invitation's generation must stay deliverable. It has no normal session.
        if not user.requires_password_change:
            user.credential_version += 1
        await session.commit()
    return member_out(user)


async def resend_member_first_access(
    session: AsyncSession, principal: Principal, user_id: UUID
) -> TeamInviteResult:
    url = staff_login_url()
    tenant = await lock_clinic(session, principal)
    user = await target_member(session, tenant.id, user_id)
    if user.access_revoked_at or (not user.requires_password_change and not user.invite_token_hash):
        raise HTTPException(409, "first_access_not_pending")
    password = generate_temporary_password()
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
