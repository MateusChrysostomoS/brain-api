"""Entitlements endpoint (CONTRACTS.md §3.1, stripe-billing-entitlements skill).

`GET /entitlements` is the single source of truth the portal calls after login to decide
which products to show/link and what plan/limits apply. The tenant is resolved
SERVER-SIDE from the validated JWT's `tenant_id` (never from client input), per
auth-jwt-multitenant. The entitlement is read from the LOCAL `entitlements` row — there
is NO Stripe / network call in this path.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, require_tenant
from brain_api.core.database import get_session
from brain_api.core.invite_codes import generate_invite_code, invite_link
from brain_api.core.logging import get_logger
from brain_api.models import Tenant
from brain_api.schemas.entitlement import EntitlementOut, PatientInviteOut
from brain_api.services.entitlements import resolve_entitlement

logger = get_logger(__name__)

# `main.py` imports `entitlements.router`; this module-level name MUST be `router`.
router = APIRouter()


@router.get("/entitlements", response_model=EntitlementOut)
async def get_entitlements(
    principal: Principal = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> EntitlementOut:
    """Return the resolved entitlement state for the authenticated tenant.

    `require_tenant` rejects platform `admin` tokens (no tenant_id) with 409. The tenant
    is taken from `principal.tenant_id` (the validated token), never from a query param
    or any client-supplied id. `resolve_entitlement` reads the local DB only.
    """
    logger.info("entitlements_read", tenant_id=str(principal.tenant_id))
    return await resolve_entitlement(session, principal.tenant_id)


@router.get("/entitlements/patient-invite", response_model=PatientInviteOut)
async def get_patient_invite(
    principal: Principal = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> PatientInviteOut:
    """The clinic's own patient invite: its short code and link (Brain-Message portal).

    Staff-only and tenant-scoped from the validated token, exactly like `GET /entitlements`.
    A tenant inserted by the previous brain-api during the deploy window has no code yet
    (the column default lives in the new model); it is minted here on first read.
    """
    tenant = await session.get(Tenant, principal.tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    if tenant.patient_invite_code is None:
        # Two staff reads racing on such a tenant must not hand out two codes: only the first
        # conditional write lands, and both answers re-read what was stored.
        await session.execute(
            update(Tenant)
            .where(Tenant.id == tenant.id, Tenant.patient_invite_code.is_(None))
            .values(patient_invite_code=generate_invite_code())
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        await session.refresh(tenant)
    logger.info("patient_invite_read", tenant_id=str(tenant.id))
    return PatientInviteOut(
        tenant_id=tenant.id,
        invite_code=tenant.patient_invite_code,
        brain_message_enabled=tenant.brain_message_enabled,
        invite_link=invite_link(tenant.patient_invite_code),
    )
