"""PreCheck SSO token issuance — the brain -> PreCheck identity bridge.

`POST /sso/precheck/token` calls this. It is the ONLY place brain-api mints a
PreCheck-compatible session, and it gates that minting on two checks before minting:

1. Entitlement (stripe-billing-entitlements skill): the acting tenant must own PreCheck.
   Resolved IN-PROCESS from the local entitlements row (the same logic GET /entitlements
   uses) — never a Stripe call. Not entitled -> 403 `precheck_not_entitled`.
2. Account link (the identity map lives server-side in brain's DB; tenant-secrets-encryption
   never-leak posture): the brain user must have a `precheck_account_links` row. No link ->
   409 `precheck_account_not_linked`, a typed signal the portal turns into a clear message
   ("ask your admin to connect your PreCheck account") instead of crashing.

Only when both pass do we mint with `create_precheck_token` (sub = the linked INTEGER
PreCheck user id). The token is short-lived and carries no brain identity or secrets — only
what PreCheck's verifier reads (auth-jwt-multitenant). The detail strings above are stable,
machine-readable codes the frontend branches on (do not localize them here).
"""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal
from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.core.security import create_precheck_token
from brain_api.models import PrecheckAccountLink
from brain_api.services.entitlements import resolve_entitlement

logger = get_logger(__name__)


@dataclass(frozen=True)
class PrecheckToken:
    """A minted PreCheck session token + its lifetime in seconds."""

    token: str
    expires_in: int


async def issue_precheck_token(session: AsyncSession, principal: Principal) -> PrecheckToken:
    """Mint a PreCheck session for an entitled, linked brain user.

    Raises HTTPException 403 (`precheck_not_entitled`) or 409
    (`precheck_account_not_linked`) per the module docstring. The endpoint guarantees
    `principal.tenant_id is not None` via the `require_tenant` dependency.
    """
    assert principal.tenant_id is not None  # enforced by require_tenant on the route

    # 1) Entitlement gate — local, in-process; identical source to GET /entitlements.
    ent = await resolve_entitlement(session, principal.tenant_id)
    if not ent.products.precheck:
        logger.info("precheck_sso_denied_not_entitled", tenant_id=str(principal.tenant_id))
        raise HTTPException(status.HTTP_403_FORBIDDEN, "precheck_not_entitled")

    # 2) Account-link gate — the brain user must be mapped to a PreCheck user.
    link = await session.scalar(
        select(PrecheckAccountLink).where(
            PrecheckAccountLink.brain_user_id == UUID(principal.user_id)
        )
    )
    if link is None:
        logger.info(
            "precheck_sso_denied_not_linked",
            tenant_id=str(principal.tenant_id),
            brain_user_id=principal.user_id,
        )
        raise HTTPException(status.HTTP_409_CONFLICT, "precheck_account_not_linked")

    # Defense in depth: a link must belong to the tenant the user is acting for. A mismatch
    # is a data-integrity anomaly — refuse to mint rather than cross a tenant boundary.
    if link.tenant_id != principal.tenant_id:
        logger.warning(
            "precheck_sso_link_tenant_mismatch",
            tenant_id=str(principal.tenant_id),
            link_tenant_id=str(link.tenant_id),
            brain_user_id=principal.user_id,
        )
        raise HTTPException(status.HTTP_409_CONFLICT, "precheck_account_not_linked")

    token = create_precheck_token(link.precheck_user_id)
    expires_in = get_settings().PRECHECK_TOKEN_EXPIRE_MINUTES * 60
    logger.info(
        "precheck_sso_issued",
        tenant_id=str(principal.tenant_id),
        brain_user_id=principal.user_id,
        precheck_user_id=link.precheck_user_id,
    )
    return PrecheckToken(token=token, expires_in=expires_in)
