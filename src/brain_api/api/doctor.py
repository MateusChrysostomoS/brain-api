"""Doctor (tenant) endpoints (RBAC task, Part 1B) — `auth-jwt-multitenant` skill.

EVERY route here is gated by `require_doctor` at the router level: the JWT must be valid,
carry a `tenant_id`, and have role `doctor`, `manager` or `secretary` (LEGACY:
`tenant_owner`/`tenant_staff` on a not-yet-expired pre-taxonomy token). A platform `admin`
token gets `403` (wrong portal). The tenant is ALWAYS taken from the token
(`principal.tenant_id`) — `tenant_id` is never accepted as a query/body param, so a doctor
cannot read another tenant's data by forging an id.

`/doctor/appointments` and `/doctor/patients` call secretaria's INTERNAL-ONLY `/internal/*`
surface over `X-Internal-Api-Key` (`services/secretaria_internal.py`), scoped to this
tenant; they degrade to an empty page when the secretaria mesh is unconfigured locally.
`/doctor/anamneses` is proxied to PreCheck (which re-validates the forwarded brain JWT).

EXCEPTION to the router-level gate: the two `/doctor/anamneses*` routes are the only
CLINICAL surface in this module (PreCheck records), so they call `deny_secretary` — the
`secretary` role is secretarIA-only and must never read patient anamneses, even though
`require_doctor` lets it into every other route here. TASK-011 adds three more clinical routes
(media list, media URL, status) under the same guard.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, deny_secretary, require_doctor
from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.core.security import create_hub_token
from brain_api.schemas.doctor import AnamnesisStatusIn, DoctorMeOut, DoctorMeUpdateIn, HubTokenOut
from brain_api.services import precheck_client, secretaria_internal
from brain_api.services.doctor import get_doctor_me, update_doctor_me
from brain_api.services.entitlements import ACTIVE_STATUSES, resolve_entitlement

logger = get_logger(__name__)

# Router-level gate: all /doctor/* require a doctor/manager token (403 else).
router = APIRouter(prefix="/doctor", dependencies=[Depends(require_doctor)])


@router.get("/me", response_model=DoctorMeOut, summary="Current doctor profile")
async def doctor_me(
    principal: Principal = Depends(require_doctor),
    session: AsyncSession = Depends(get_session),
) -> DoctorMeOut:
    """The authenticated doctor's profile + tenant + entitlements (no secrets)."""
    logger.info("doctor_me", tenant_id=str(principal.tenant_id))
    return await get_doctor_me(session, principal)


@router.patch("/me", response_model=DoctorMeOut, summary="Self-edit low-risk profile fields")
async def doctor_me_update(
    payload: DoctorMeUpdateIn,
    principal: Principal = Depends(require_doctor),
    session: AsyncSession = Depends(get_session),
) -> DoctorMeOut:
    """Edit the CALLER'S OWN low-risk fields (today: `name` only — "Meu Perfil" foundation).

    Email (login key + PreCheck SSO identity), role, tenant, and password are never
    accepted here: `DoctorMeUpdateIn` uses `extra="forbid"`, so a payload that includes
    any of them is rejected with 422 by the schema itself, before this handler runs.
    Returns the refreshed `DoctorMeOut` (same shape as `GET /doctor/me`).
    """
    logger.info("doctor_me_update", tenant_id=str(principal.tenant_id))
    return await update_doctor_me(session, principal, payload)


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
    return await secretaria_internal.list_appointments(principal.tenant_id, skip=skip, limit=limit)


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
    return await secretaria_internal.list_patients(principal.tenant_id, skip=skip, limit=limit)


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

    Clinical data: refused for a `secretary` (403 `secretary_precheck_not_allowed`).
    """
    deny_secretary(principal, "secretary_precheck_not_allowed")
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

    Clinical data: refused for a `secretary` (403 `secretary_precheck_not_allowed`).
    """
    deny_secretary(principal, "secretary_precheck_not_allowed")
    logger.info(
        "doctor_anamnesis_detail_proxy",
        tenant_id=str(principal.tenant_id),
        anamnesis_id=anamnesis_id,
    )
    return await precheck_client.get_anamnesis(authorization or "", anamnesis_id)


@router.get("/anamneses/{anamnesis_id}/media", summary="Anamnesis media list (proxied)")
async def anamnesis_media(
    anamnesis_id: int,
    authorization: str | None = Header(default=None),
    principal: Principal = Depends(require_doctor),
) -> object:
    """Media ids of one anamnesis (brain-api -> precheck `.../anamneses/{id}/media`).

    PreCheck scopes by the forwarded token's clinic (404 outside it). No URL here.
    Clinical data: refused for a `secretary` (403 `secretary_precheck_not_allowed`).
    """
    deny_secretary(principal, "secretary_precheck_not_allowed")
    logger.info(
        "doctor_anamnesis_media_proxy",
        tenant_id=str(principal.tenant_id),
        anamnesis_id=anamnesis_id,
    )
    return await precheck_client.list_anamnesis_media(authorization or "", anamnesis_id)


@router.get("/anamneses/media/{media_id}/url", summary="Signed URL of one anamnesis media")
async def anamnesis_media_url(
    media_id: int,
    authorization: str | None = Header(default=None),
    principal: Principal = Depends(require_doctor),
) -> object:
    """Short-lived signed URL (brain-api -> precheck `.../anamneses/media/{id}/url`).

    Passthrough only: the file is never downloaded here, and the URL (a bearer
    credential for ~15 min) is never logged — only the media id is.
    Clinical data: refused for a `secretary` (403 `secretary_precheck_not_allowed`).
    """
    deny_secretary(principal, "secretary_precheck_not_allowed")
    logger.info(
        "doctor_anamnesis_media_url_proxy",
        tenant_id=str(principal.tenant_id),
        media_id=media_id,
    )
    return await precheck_client.get_anamnesis_media_url(authorization or "", media_id)


@router.patch("/anamneses/{anamnesis_id}/status", summary="Triage an anamnesis (proxied)")
async def anamnesis_status(
    anamnesis_id: int,
    payload: AnamnesisStatusIn,
    authorization: str | None = Header(default=None),
    principal: Principal = Depends(require_doctor),
) -> object:
    """Mark one anamnesis `approved` | `rejected` (brain-api -> precheck `.../status`).

    PreCheck writes only `summaries.status` and 404s a record outside the token's clinic.
    Unconfigured PreCheck is a 503 — a write is never faked.
    Clinical data: refused for a `secretary` (403 `secretary_precheck_not_allowed`).
    """
    deny_secretary(principal, "secretary_precheck_not_allowed")
    logger.info(
        "doctor_anamnesis_status_proxy",
        tenant_id=str(principal.tenant_id),
        anamnesis_id=anamnesis_id,
        status=payload.status,
    )
    return await precheck_client.set_anamnesis_status(
        authorization or "", anamnesis_id, payload.status
    )
