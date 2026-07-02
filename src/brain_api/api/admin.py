"""Platform-admin endpoints (RBAC task, Part 1A) — `auth-jwt-multitenant` skill.

EVERY route here is gated by `require_role("admin")` declared at the router level, so a
non-admin JWT gets `403` before any handler runs and no admin route can be added without
the gate. Admins are platform-level (no `tenant_id`) and act across all tenants.

Responses serialize through whitelisted `*Out` schemas (no `password_hash`, no
`*_encrypted`) per `tenant-secrets-encryption`. Mutations log a stable actor reference
(`principal.user_id`) — never the email, token, or password.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, get_current_principal, require_role
from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.schemas.admin import (
    AdminDemoRequestOut,
    AdminDemoRequestPatchIn,
    AdminTenantDetailOut,
    AdminTenantOut,
    AdminUserCreateIn,
    AdminUserOut,
    EntitlementAdminOut,
    EntitlementPatchIn,
    ImpersonationTokenOut,
    Page,
)
from brain_api.schemas.secretaria import SecretariaResetIn
from brain_api.services import admin as admin_service, precheck_client, secretaria_client

logger = get_logger(__name__)

# Router-level gate: all /admin/* require an `admin` role (403 otherwise).
router = APIRouter(prefix="/admin", dependencies=[Depends(require_role("admin"))])


# --- Tenants ---------------------------------------------------------------


@router.get("/tenants", response_model=Page[AdminTenantOut], summary="List tenants")
async def list_tenants(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> Page[AdminTenantOut]:
    """All tenants, newest first, with plan/product flags + user count."""
    items, total = await admin_service.list_tenants(session, skip, limit)
    return Page(items=items, total=total, skip=skip, limit=limit)


@router.get(
    "/tenants/{tenant_id}",
    response_model=AdminTenantDetailOut,
    summary="Tenant detail",
    responses={404: {"description": "Tenant not found."}},
)
async def get_tenant(
    tenant_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> AdminTenantDetailOut:
    """Full tenant detail + entitlement record. No credentials fields."""
    return await admin_service.get_tenant_detail(session, tenant_id)


@router.get(
    "/tenants/{tenant_id}/entitlements",
    response_model=EntitlementAdminOut,
    summary="Read tenant entitlements",
    responses={404: {"description": "Tenant not found."}},
)
async def get_tenant_entitlements(
    tenant_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> EntitlementAdminOut:
    """The entitlement record for one tenant (coherent defaults if no row yet)."""
    return await admin_service.get_entitlement(session, tenant_id)


@router.patch(
    "/tenants/{tenant_id}/entitlements",
    response_model=EntitlementAdminOut,
    summary="Update tenant entitlements",
    responses={404: {"description": "Tenant not found."}},
)
async def patch_tenant_entitlements(
    tenant_id: UUID,
    patch: EntitlementPatchIn,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> EntitlementAdminOut:
    """Manually activate/deactivate products or set plan/status (MVP, pre-Stripe)."""
    result = await admin_service.update_entitlement(session, tenant_id, patch)
    logger.info(
        "admin_entitlement_updated",
        actor_user_id=principal.user_id,
        tenant_id=str(tenant_id),
    )
    return result


# --- Users -----------------------------------------------------------------


@router.get("/users", response_model=Page[AdminUserOut], summary="List users")
async def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> Page[AdminUserOut]:
    """All users across all tenants, newest first. Never includes `password_hash`."""
    items, total = await admin_service.list_users(session, skip, limit)
    return Page(items=items, total=total, skip=skip, limit=limit)


@router.post(
    "/users",
    response_model=AdminUserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create user",
    responses={
        404: {"description": "Named tenant not found."},
        409: {"description": "Email already registered."},
        422: {"description": "Bad role/tenant combination or password too long."},
    },
)
async def create_user(
    payload: AdminUserCreateIn,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    """Create a user in any tenant with any role (how we onboard doctor accounts)."""
    user = await admin_service.create_user(session, payload)
    logger.info(
        "admin_user_created",
        actor_user_id=principal.user_id,
        created_user_id=str(user.id),
        role=user.role,
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
    )
    return user


# --- Demo requests ---------------------------------------------------------


@router.get(
    "/demo_requests",
    response_model=Page[AdminDemoRequestOut],
    summary="List demo requests",
)
async def list_demo_requests(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> Page[AdminDemoRequestOut]:
    """All demo requests, newest first (brain's own `demo_requests` table)."""
    items, total = await admin_service.list_demo_requests(session, skip, limit)
    return Page(items=items, total=total, skip=skip, limit=limit)


@router.patch(
    "/demo_requests/{demo_id}",
    response_model=AdminDemoRequestOut,
    summary="Update demo request status",
    responses={404: {"description": "Demo request not found."}},
)
async def patch_demo_request(
    demo_id: UUID,
    patch: AdminDemoRequestPatchIn,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> AdminDemoRequestOut:
    """Mark a demo request `contacted` / `converted` / `dismissed`."""
    result = await admin_service.update_demo_request(session, demo_id, patch.status)
    logger.info(
        "admin_demo_request_updated",
        actor_user_id=principal.user_id,
        demo_request_id=str(demo_id),
        new_status=patch.status,
    )
    return result


# --- Doctor-mode impersonation ("Modo médico") -----------------------------


@router.post(
    "/impersonate/token",
    response_model=ImpersonationTokenOut,
    summary="Mint a doctor session for the admin 'Modo médico' switch",
    responses={
        404: {"description": "Configured impersonation target clinic not found/seeded."},
    },
)
async def impersonate_token(
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> ImpersonationTokenOut:
    """Mint a tenant-scoped doctor token so an admin can enter the doctor portal ("Modo
    médico") with REAL PreCheck/secretarIA data — for developing the website + API.

    One-click: it always targets the configured demo clinic (`IMPERSONATION_DEMO_EMAIL`);
    the admin does not choose a tenant. The router-level `require_role("admin")` gate is the
    sole authorization. Each mint is logged at WARNING with the acting admin + target user /
    tenant (the minted token itself is NEVER logged). This deliberately crosses the
    "admin SSO is not wired" boundary (CONTRACTS §11.3 / §11.4) for an admin-only dev tool;
    the issued token is shape-identical to that doctor's own login. Returns
    `404 impersonation_target_unavailable` if the demo clinic is not seeded/configured.
    """
    target_email = get_settings().IMPERSONATION_DEMO_EMAIL
    result = await admin_service.issue_impersonation_token(session, target_email)
    logger.warning(
        "admin_impersonation_issued",
        actor_user_id=principal.user_id,
        target_user_id=str(result.target_user_id),
        target_tenant_id=str(result.out.tenant_id),
        target_role=result.out.role,
    )
    return result.out


# --- Inbound (proxied from PreCheck) ---------------------------------------


@router.get("/inbound", summary="PreCheck inbound leads (proxied)")
async def get_inbound(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    authorization: str | None = Header(default=None),
    principal: Principal = Depends(get_current_principal),
) -> object:
    """Proxy the PreCheck admin inbound page (brain-api -> precheck `/api/v1/admin/inbound`).

    The admin's own brain JWT is forwarded; PreCheck re-validates it and re-checks the
    `admin` role on its side. Returns PreCheck's payload verbatim.
    """
    logger.info("admin_inbound_proxy", actor_user_id=principal.user_id)
    return await precheck_client.get_inbound(authorization or "", skip, limit)


# --- secretaria (service-to-service admin connection) ----------------------
# secretaria has no user identity to authorize against, so unlike the PreCheck proxy above
# (which forwards the caller's JWT) these forward NOTHING from the caller — brain-api
# authenticates to secretaria with the shared SECRETARIA_ADMIN_TOKEN. The brain `admin`
# role is enforced by the router-level gate; the service credential lives in the client.


@router.get("/secretaria/tenants", summary="secretaria tenants (proxied admin)")
async def get_secretaria_tenants(
    principal: Principal = Depends(get_current_principal),
) -> object:
    """List secretaria clinics + calendar health (brain-api -> secretaria `/admin/tenants`).

    Returns secretaria's payload verbatim. 503 if secretaria's base URL or admin token is
    not configured on brain-api; 502 if secretaria is unreachable. The shared admin token is
    never logged or echoed.
    """
    logger.info("admin_secretaria_tenants_proxy", actor_user_id=principal.user_id)
    return await secretaria_client.list_tenants()


@router.post(
    "/secretaria/reset",
    summary="Wipe secretaria conversation data (DESTRUCTIVE)",
    responses={
        400: {"description": "confirm was not true."},
        502: {"description": "secretaria unreachable."},
        503: {"description": "secretaria base URL / admin token not configured."},
    },
)
async def reset_secretaria(
    payload: SecretariaResetIn,
    principal: Principal = Depends(get_current_principal),
) -> object:
    """Proxy secretaria's destructive data wipe (brain-api -> secretaria `/admin/reset`).

    TWO independent guards run before anything is wiped: the brain `admin` role (router
    gate) and an explicit `confirm: true` body here. The actor is logged at WARNING; the
    shared admin token is never logged or echoed.
    """
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set confirm: true to proceed.")
    logger.warning(
        "admin_secretaria_reset_invoked",
        actor_user_id=principal.user_id,
        include_tenants=payload.include_tenants,
    )
    return await secretaria_client.reset_data(include_tenants=payload.include_tenants)
