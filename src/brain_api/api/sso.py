"""SSO endpoint (CONTRACTS.md §5, auth-jwt-multitenant skill).

`POST /sso/precheck/token` is the brain -> PreCheck bridge: an authenticated, PreCheck-
entitled, linked brain user exchanges their brain JWT for a PreCheck-compatible token so
the ported PreCheck dashboard accepts them without a second login. The tenant is taken
from the validated brain JWT (`require_tenant`), never from client input; all gating and
minting happen in the service.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, require_tenant
from brain_api.core.database import get_session
from brain_api.schemas.sso import PrecheckSsoTokenResponse
from brain_api.services.sso import issue_precheck_token

# `main.py` imports `sso.router`; this module-level name MUST be `router`.
router = APIRouter(prefix="/sso")


@router.post("/precheck/token", response_model=PrecheckSsoTokenResponse)
async def precheck_sso_token(
    principal: Principal = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> PrecheckSsoTokenResponse:
    """Mint a PreCheck session for the authenticated tenant's linked user.

    `require_tenant` rejects platform admin tokens (no tenant) with 409. The service
    returns 403 `precheck_not_entitled` if the tenant does not own PreCheck, or 409
    `precheck_account_not_linked` if the brain user has no PreCheck link.
    """
    result = await issue_precheck_token(session, principal)
    return PrecheckSsoTokenResponse(token=result.token, expires_in=result.expires_in)
