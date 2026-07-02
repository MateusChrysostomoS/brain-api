"""Admin service layer — cross-tenant reads/writes for platform admins (Part 1A).

Backs the `/admin/*` endpoints, which are role-gated to `admin` in `api/admin.py`. By
design these functions are tenant-agnostic: an admin acts across every tenant. Writes
commit explicitly (the request session is not auto-committing), mirroring `services/demo`.

Nothing here returns a `password_hash` or any secret — callers serialize through the
whitelisted `*Out` schemas in `schemas/admin.py`.
"""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.security import create_access_token, hash_password
from brain_api.models import DemoRequest, Entitlement, Tenant, User
from brain_api.models.user import ROLE_TENANT_OWNER, ROLE_TENANT_STAFF
from brain_api.schemas.admin import (
    AdminDemoRequestOut,
    AdminTenantDetailOut,
    AdminTenantOut,
    AdminUserCreateIn,
    AdminUserOut,
    EntitlementAdminOut,
    EntitlementPatchIn,
    ImpersonationTokenOut,
)


def _entitlement_out(tenant_id: UUID, ent: Entitlement | None) -> EntitlementAdminOut:
    """Project an Entitlement row (or a coherent default when absent) to its admin view.

    Mirrors the "never 404 a valid tenant for entitlements" rule: a tenant without a row
    reads as all-products-off / free / inactive rather than an error.
    """
    if ent is None:
        return EntitlementAdminOut(
            tenant_id=tenant_id,
            precheck_enabled=False,
            secretaria_enabled=False,
            plan="free",
            status="inactive",
        )
    return EntitlementAdminOut.model_validate(ent)


# --- Tenants ---------------------------------------------------------------


async def list_tenants(
    session: AsyncSession, skip: int, limit: int
) -> tuple[list[AdminTenantOut], int]:
    """Return one page of tenants (newest first) + the total count.

    Assembles each row from three small queries (tenant page, their entitlement flags,
    their user counts) rather than one entity+aggregate+group_by query — robust across
    SQLite (tests) and Postgres (prod), and trivial at MVP scale.
    """
    total = await session.scalar(select(func.count()).select_from(Tenant)) or 0
    tenants = (
        await session.scalars(
            select(Tenant).order_by(Tenant.created_at.desc()).offset(skip).limit(limit)
        )
    ).all()
    if not tenants:
        return [], total

    ids = [t.id for t in tenants]
    ents = {
        e.tenant_id: e
        for e in (
            await session.scalars(select(Entitlement).where(Entitlement.tenant_id.in_(ids)))
        ).all()
    }
    counts = {
        tid: count
        for tid, count in (
            await session.execute(
                select(User.tenant_id, func.count(User.id))
                .where(User.tenant_id.in_(ids))
                .group_by(User.tenant_id)
            )
        ).all()
    }

    items = [
        AdminTenantOut(
            id=t.id,
            clinic_name=t.clinic_name,
            created_at=t.created_at,
            plan=ents[t.id].plan if t.id in ents else "free",
            status=ents[t.id].status if t.id in ents else "inactive",
            precheck_enabled=ents[t.id].precheck_enabled if t.id in ents else False,
            secretaria_enabled=ents[t.id].secretaria_enabled if t.id in ents else False,
            users_count=counts.get(t.id, 0),
        )
        for t in tenants
    ]
    return items, total


async def get_tenant_detail(session: AsyncSession, tenant_id: UUID) -> AdminTenantDetailOut:
    """Full tenant detail + its entitlement record. 404 if the tenant does not exist."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    ent = await session.get(Entitlement, tenant_id)
    users_count = (
        await session.scalar(
            select(func.count()).select_from(User).where(User.tenant_id == tenant_id)
        )
        or 0
    )
    return AdminTenantDetailOut(
        id=tenant.id,
        clinic_name=tenant.clinic_name,
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
        users_count=users_count,
        entitlements=_entitlement_out(tenant_id, ent),
    )


# --- Entitlements ----------------------------------------------------------


async def get_entitlement(session: AsyncSession, tenant_id: UUID) -> EntitlementAdminOut:
    """Read a tenant's entitlement record (defaults when no row). 404 if no such tenant."""
    if await session.get(Tenant, tenant_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    ent = await session.get(Entitlement, tenant_id)
    return _entitlement_out(tenant_id, ent)


async def update_entitlement(
    session: AsyncSession, tenant_id: UUID, patch: EntitlementPatchIn
) -> EntitlementAdminOut:
    """Apply a partial entitlement update (manual product activation, MVP).

    Upserts: a tenant with no entitlement row yet gets one created. Only the fields the
    client actually sent are applied (`exclude_unset`); a `null` for a non-nullable
    column is ignored rather than written.
    """
    if await session.get(Tenant, tenant_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")

    ent = await session.get(Entitlement, tenant_id)
    if ent is None:
        ent = Entitlement(tenant_id=tenant_id)
        session.add(ent)

    for key, value in patch.model_dump(exclude_unset=True).items():
        if value is not None:
            # Reassigning the whole dict (addons/limits) is what triggers change
            # tracking; in-place edits to a JSON column would not persist.
            setattr(ent, key, value)

    await session.commit()
    await session.refresh(ent)
    return EntitlementAdminOut.model_validate(ent)


# --- Users -----------------------------------------------------------------


async def list_users(
    session: AsyncSession, skip: int, limit: int
) -> tuple[list[AdminUserOut], int]:
    """Return one page of users (newest first) + the total count.

    Left-joins the tenant so the table can show the clinic name (or "Platform Admin" for
    a null tenant). `password_hash` is never selected into the response shape.
    """
    total = await session.scalar(select(func.count()).select_from(User)) or 0
    rows = (
        await session.execute(
            select(User, Tenant.clinic_name)
            .outerjoin(Tenant, User.tenant_id == Tenant.id)
            .order_by(User.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
    ).all()
    items = [
        AdminUserOut(
            id=user.id,
            tenant_id=user.tenant_id,
            clinic_name=clinic_name,
            email=user.email,
            name=user.name,
            role=user.role,
            created_at=user.created_at,
        )
        for user, clinic_name in rows
    ]
    return items, total


async def create_user(session: AsyncSession, payload: AdminUserCreateIn) -> AdminUserOut:
    """Create a user in any tenant with any role. Email is stored lower-cased.

    409 if the email already exists; 404 if a named tenant does not exist. The password
    is bcrypt-hashed here and never returned.
    """
    email = payload.email.lower()
    if await session.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    clinic_name: str | None = None
    if payload.tenant_id is not None:
        tenant = await session.get(Tenant, payload.tenant_id)
        if tenant is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
        clinic_name = tenant.clinic_name

    user = User(
        tenant_id=payload.tenant_id,
        email=email,
        name=payload.name,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return AdminUserOut(
        id=user.id,
        tenant_id=user.tenant_id,
        clinic_name=clinic_name,
        email=user.email,
        name=user.name,
        role=user.role,
        created_at=user.created_at,
    )


# --- Demo requests ---------------------------------------------------------


async def list_demo_requests(
    session: AsyncSession, skip: int, limit: int
) -> tuple[list[AdminDemoRequestOut], int]:
    """Return one page of demo requests (newest first) + the total count."""
    total = await session.scalar(select(func.count()).select_from(DemoRequest)) or 0
    rows = (
        await session.scalars(
            select(DemoRequest).order_by(DemoRequest.created_at.desc()).offset(skip).limit(limit)
        )
    ).all()
    return [AdminDemoRequestOut.model_validate(r) for r in rows], total


async def update_demo_request(
    session: AsyncSession, demo_id: UUID, new_status: str
) -> AdminDemoRequestOut:
    """Move a demo request to `contacted` / `converted` / `dismissed`. 404 if missing."""
    row = await session.get(DemoRequest, demo_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Demo request not found")
    row.status = new_status
    await session.commit()
    await session.refresh(row)
    return AdminDemoRequestOut.model_validate(row)


# --- Doctor-mode impersonation ("Modo médico") -----------------------------


@dataclass(frozen=True)
class ImpersonationMint:
    """The minted doctor session plus the target user id (for the audit log).

    `target_user_id` is kept OUT of the response (it is not in `ImpersonationTokenOut`) and
    handed back separately so the route can log a stable actor→target reference without ever
    serializing an internal id to the client.
    """

    out: ImpersonationTokenOut
    target_user_id: UUID


async def issue_impersonation_token(
    session: AsyncSession, target_email: str
) -> ImpersonationMint:
    """Mint a tenant-scoped doctor token for the admin "Modo médico" handoff.

    Resolves `target_email` (the configured demo clinic owner) to a tenant doctor user and
    mints a NORMAL access token for them — `create_access_token(sub=user, tenant_id, role)`,
    byte-identical in shape to that user's own login — so `/doctor/*`, `/entitlements` and
    the PreCheck SSO accept it without any special-casing.

    Refuses with **404 `impersonation_target_unavailable`** when the target does not exist,
    has no tenant, or is not a doctor role: an admin must never become a "doctor with no
    tenant", which would violate `require_doctor`'s invariant. Read-only (mints, never
    writes); the caller logs the impersonation.
    """
    email = target_email.strip().lower()
    user = await session.scalar(select(User).where(User.email == email))
    if (
        user is None
        or user.tenant_id is None
        or user.role not in (ROLE_TENANT_OWNER, ROLE_TENANT_STAFF)
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "impersonation_target_unavailable"
        )
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "impersonation_target_unavailable"
        )

    settings = get_settings()
    token = create_access_token(
        sub=str(user.id), tenant_id=str(user.tenant_id), role=user.role
    )
    out = ImpersonationTokenOut(
        access_token=token,
        tenant_id=user.tenant_id,
        clinic_name=tenant.clinic_name,
        email=user.email,
        role=user.role,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return ImpersonationMint(out=out, target_user_id=user.id)
