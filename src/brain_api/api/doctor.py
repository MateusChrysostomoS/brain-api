"""Doctor (tenant) endpoints (RBAC task, Part 1B) — `auth-jwt-multitenant` skill.

EVERY route here is gated by `require_doctor` at the router level: the JWT must be valid,
carry a `tenant_id`, and have role `tenant_owner` or `tenant_staff`. A platform `admin`
token gets `403` (wrong portal). The tenant is ALWAYS taken from the token
(`principal.tenant_id`) — `tenant_id` is never accepted as a query/body param, so a doctor
cannot read another tenant's data by forging an id.

`/doctor/appointments` and `/doctor/patients` call secretaria's INTERNAL-ONLY `/internal/*`
surface over `X-Internal-Api-Key` (`services/secretaria_internal.py`), scoped to this
tenant; they degrade to an empty page when the secretaria mesh is unconfigured locally.
`/doctor/anamneses` is proxied to PreCheck (which re-validates the forwarded brain JWT).
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, require_doctor
from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.core.security import create_hub_token
from brain_api.schemas.doctor import DoctorMeOut, HubTokenOut
from brain_api.services import precheck_client, secretaria_internal
from brain_api.services.doctor import get_doctor_me
from brain_api.services.entitlements import ACTIVE_STATUSES, resolve_entitlement

logger = get_logger(__name__)

# Router-level gate: all /doctor/* require a tenant_owner/tenant_staff token (403 else).
router = APIRouter(prefix="/doctor", dependencies=[Depends(require_doctor)])


@router.get("/me", response_model=DoctorMeOut, summary="Current doctor profile")
async def doctor_me(
    principal: Principal = Depends(require_doctor),
    session: AsyncSession = Depends(get_session),
) -> DoctorMeOut:
    """The authenticated doctor's profile + tenant + entitlements (no secrets)."""
    logger.info("doctor_me", tenant_id=str(principal.tenant_id))
    return await get_doctor_me(session, principal)


@router.get("/appointments", summary="Tenant appointments")
async def appointments(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    principal: Principal = Depends(require_doctor),
) -> object:
    """Appointments for the doctor's tenant (brain-api -> secretaria `/internal`).

    Scoped to `principal.tenant_id` from the validated token — `tenant_id` is never a
    client param, so a doctor cannot read another tenant's appointments. Degrades to an
    empty page when the secretaria mesh is unconfigured locally; upstream/key failures
    surface as `502` (never secretaria's body, never a config issue as the doctor's 401).
    """
    logger.info("doctor_appointments", tenant_id=str(principal.tenant_id))
    return await secretaria_internal.list_appointments(
        principal.tenant_id, skip=skip, limit=limit
    )


@router.get("/patients", summary="Tenant patients")
async def patients(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    principal: Principal = Depends(require_doctor),
) -> object:
    """Patients for the doctor's tenant (brain-api -> secretaria `/internal`).

    Same tenant-scoping and fail-closed behaviour as `appointments`.
    """
    logger.info("doctor_patients", tenant_id=str(principal.tenant_id))
    return await secretaria_internal.list_patients(
        principal.tenant_id, skip=skip, limit=limit
    )


@router.post(
    "/secretaria/hub-token",
    response_model=HubTokenOut,
    summary="Mint a secretarIA hub session for this tenant",
    responses={
        403: {"description": "Tenant not entitled to secretarIA (or admin token)."},
    },
)
async def secretaria_hub_token(
    principal: Principal = Depends(require_doctor),
    session: AsyncSession = Depends(get_session),
) -> HubTokenOut:
    """Mint the tenant-scoped, purpose-scoped token the portal presents to secretarIA's
    doctor hub (NOT the doctor's own JWT — no user JWT ever reaches secretarIA).

    Entitlement-gated like the PreCheck SSO mint: the tenant must be active/trialing
    AND have secretaria enabled, else 403 `secretaria_not_entitled`. The gate is
    re-checked LIVE on every hub request anyway (secretarIA introspects the token via
    /internal/secretaria/hub-token/verify), so a cancellation mid-session locks the hub
    within one request. The minted token is never logged.
    """
    ent = await resolve_entitlement(session, principal.tenant_id)
    if ent.status not in ACTIVE_STATUSES or not ent.products.secretaria:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "secretaria_not_entitled")
    token = create_hub_token(
        tenant_id=str(principal.tenant_id),
        actor_user_id=principal.user_id,
        professional_id=str(principal.professional_id) if principal.professional_id else None,
    )
    logger.info("hub_token_minted", tenant_id=str(principal.tenant_id))
    return HubTokenOut(
        hub_token=token,
        expires_in=get_settings().HUB_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.get("/anamneses", summary="Tenant anamneses (proxied from PreCheck)")
async def anamneses(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    authorization: str | None = Header(default=None),
    principal: Principal = Depends(require_doctor),
) -> object:
    """List the tenant's anamnesis records (brain-api -> precheck `/api/v1/doctor/anamneses`).

    The doctor's brain JWT is forwarded; PreCheck re-validates it and scopes records to
    the tenant's clinic. brain-api never sends `tenant_id` — PreCheck derives it from the
    token. Returns PreCheck's payload verbatim.
    """
    logger.info("doctor_anamneses_proxy", tenant_id=str(principal.tenant_id))
    return await precheck_client.list_anamneses(authorization or "", skip, limit)


@router.get("/anamneses/{anamnesis_id}", summary="Anamnesis detail (proxied)")
async def anamnesis_detail(
    anamnesis_id: int,
    authorization: str | None = Header(default=None),
    principal: Principal = Depends(require_doctor),
) -> object:
    """One anamnesis record (brain-api -> precheck `/api/v1/doctor/anamneses/{id}`).

    PreCheck enforces that the record belongs to the forwarded token's tenant/clinic, so
    a doctor cannot read another tenant's anamnesis by guessing an id.
    """
    logger.info(
        "doctor_anamnesis_detail_proxy",
        tenant_id=str(principal.tenant_id),
        anamnesis_id=anamnesis_id,
    )
    return await precheck_client.get_anamnesis(authorization or "", anamnesis_id)
