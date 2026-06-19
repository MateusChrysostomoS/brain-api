"""Entitlements endpoint (CONTRACTS.md §3.1, stripe-billing-entitlements skill).

`GET /entitlements` is the single source of truth the portal calls after login to decide
which products to show/link and what plan/limits apply. The tenant is resolved
SERVER-SIDE from the validated JWT's `tenant_id` (never from client input), per
auth-jwt-multitenant. The entitlement is read from the LOCAL `entitlements` row — there
is NO Stripe / network call in this path.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, require_tenant
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.schemas.entitlement import EntitlementOut
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
