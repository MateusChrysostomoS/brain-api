"""Doctor (tenant) service layer (RBAC task, Part 1B).

Assembles the `GET /doctor/me` view. The tenant is taken from the validated JWT
(`principal.tenant_id`, guaranteed non-null by `require_doctor`) — never from client
input. Entitlements are resolved from the local DB (the same `resolve_entitlement` the
portal's `GET /entitlements` uses), so the doctor sees exactly what they may access.
"""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal
from brain_api.schemas.auth import TenantOut, UserOut
from brain_api.schemas.doctor import DoctorMeOut
from brain_api.services.auth import get_tenant, get_user
from brain_api.services.entitlements import resolve_entitlement


async def get_doctor_me(session: AsyncSession, principal: Principal) -> DoctorMeOut:
    """Resolve the doctor's profile + tenant + entitlements.

    `require_doctor` guarantees `principal.tenant_id is not None`. A token that resolves
    to a deleted user/tenant (valid signature, gone row) is treated as no longer valid.
    """
    assert principal.tenant_id is not None  # enforced by require_doctor on the route

    user = await get_user(session, UUID(principal.user_id))
    tenant = await get_tenant(session, principal.tenant_id)
    if user is None or tenant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    entitlements = await resolve_entitlement(session, principal.tenant_id)
    return DoctorMeOut(
        user=UserOut.model_validate(user),
        tenant=TenantOut.model_validate(tenant),
        entitlements=entitlements,
    )
