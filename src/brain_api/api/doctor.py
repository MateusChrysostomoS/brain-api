"""Doctor (tenant) endpoints (RBAC task, Part 1B) — `auth-jwt-multitenant` skill.

EVERY route here is gated by `require_doctor` at the router level: the JWT must be valid,
carry a `tenant_id`, and have role `tenant_owner` or `tenant_staff`. A platform `admin`
token gets `403` (wrong portal). The tenant is ALWAYS taken from the token
(`principal.tenant_id`) — `tenant_id` is never accepted as a query/body param, so a doctor
cannot read another tenant's data by forging an id.

`/doctor/appointments` and `/doctor/patients` are stubs until secretaria exposes them;
the auth+scoping pattern is wired so swapping in the real internal call is mechanical.
`/doctor/anamneses` is proxied to PreCheck (which re-validates the forwarded brain JWT).
"""

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, require_doctor
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.schemas.doctor import DoctorMeOut
from brain_api.services import precheck_client
from brain_api.services.doctor import get_doctor_me

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


@router.get("/appointments", summary="Tenant appointments (stub)")
async def appointments(
    principal: Principal = Depends(require_doctor),
) -> dict:
    """Appointments for the doctor's tenant.

    STUB: secretaria does not expose an internal appointments endpoint yet. The auth gate
    and tenant scope (`principal.tenant_id`) are in place; the real call will go
    brain-api -> secretaria over `X-Internal-Api-Key`, scoped to this tenant.
    """
    logger.info("doctor_appointments_stub", tenant_id=str(principal.tenant_id))
    return {"data": [], "stub": True}


@router.get("/patients", summary="Tenant patients (stub)")
async def patients(
    principal: Principal = Depends(require_doctor),
) -> dict:
    """Patients for the doctor's tenant.

    STUB (see `appointments`): wired with the auth gate + tenant scope; the real call
    will be proxied from secretaria, scoped to `principal.tenant_id`.
    """
    logger.info("doctor_patients_stub", tenant_id=str(principal.tenant_id))
    return {"data": [], "stub": True}


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
