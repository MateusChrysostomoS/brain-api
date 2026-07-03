"""brain-api's INBOUND service-to-service API (`/internal/*`) — INTERNAL ONLY.

The reverse direction of the existing brain-api -> secretaria data path: secretarIA
calls BACK into brain-api to (a) introspect a doctor-hub token and (b) read a tenant's
entitlement summary for its plugin gates. Same trust boundary, same shared secret PAIR:
the caller sends `X-Internal-Api-Key` = its `INTERNAL_API_KEY`, which MUST equal our
`SECRETARIA_API_KEY` byte-for-byte — one secret, both directions (CONTRACTS.md §12.1).

Gate semantics mirror secretaria's `require_internal_api_key`: fail CLOSED when our
key is unset (403), 401 on mismatch, constant-time compare, the candidate key is never
logged. During a rotation window `SECRETARIA_API_KEY_PREVIOUS` is also accepted
(verification only — docs/key-rotation.md).

No user JWT is accepted here and none is forwarded: the hub token is purpose-scoped
(`scope=secretaria_hub`) and the entitlement answer is recomputed live from the local
row (stripe-billing-entitlements: never from a token, never from Stripe).
"""

import secrets
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.core.security import decode_hub_token
from brain_api.schemas.internal import (
    HubTokenVerifyIn,
    HubTokenVerifyOut,
    InternalEntitlementOut,
    PrecheckHandoffIn,
    PrecheckHandoffOut,
    UsageEventIn,
    UsageEventOut,
)
from brain_api.services.entitlements import ACTIVE_STATUSES, resolve_entitlement
from brain_api.services.precheck_handoff import request_handoff
from brain_api.services.usage import record_usage

logger = get_logger(__name__)

_internal_key_scheme = APIKeyHeader(
    name="X-Internal-Api-Key",
    auto_error=False,
    description=(
        "Service-to-service shared secret (the brain<->secretaria pair key). "
        "Configure via SECRETARIA_API_KEY."
    ),
)


def require_internal_api_key(
    key: Annotated[str | None, Security(_internal_key_scheme)] = None,
) -> None:
    """Gate `/internal/*` on the shared pair key. Fail CLOSED; never log the candidate.

    Accepts the current key or, during a rotation window, the previous one — so the
    two services can be flipped one at a time with zero mesh downtime.
    """
    settings = get_settings()
    expected = settings.SECRETARIA_API_KEY
    if not expected:
        logger.warning("internal_auth_unconfigured")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Internal API not configured.")
    accepted = [expected]
    if settings.SECRETARIA_API_KEY_PREVIOUS:
        accepted.append(settings.SECRETARIA_API_KEY_PREVIOUS)
    if not key or not any(secrets.compare_digest(key, candidate) for candidate in accepted):
        logger.warning("internal_auth_failed")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid internal API key.")


router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_internal_api_key)],
)

_INTERNAL_RESPONSES = {
    401: {"description": "Missing or invalid X-Internal-Api-Key."},
    403: {"description": "SECRETARIA_API_KEY not configured on the server."},
}


@router.post(
    "/secretaria/hub-token/verify",
    response_model=HubTokenVerifyOut,
    summary="Introspect a secretarIA hub token (internal)",
    responses=_INTERNAL_RESPONSES,
)
async def verify_hub_token(
    payload: HubTokenVerifyIn,
    session: AsyncSession = Depends(get_session),
) -> HubTokenVerifyOut:
    """The LIVE subscription answer behind secretarIA's `verify_subscription_token`.

    `active=true` requires ALL of: a validly-signed, unexpired token with
    `scope=secretaria_hub` (a user JWT is rejected outright); entitlement status
    active/trialing; `secretaria_enabled`. Everything else is `active=false` — the
    caller fails closed. Always 200 for an authenticated service caller (the refusal
    is data, not an HTTP error).
    """
    claims = decode_hub_token(payload.token)
    if claims is None:
        return HubTokenVerifyOut(active=False)
    try:
        tenant_id = UUID(str(claims["sub"]))
    except ValueError:
        return HubTokenVerifyOut(active=False)

    ent = await resolve_entitlement(session, tenant_id)
    active = ent.status in ACTIVE_STATUSES and ent.products.secretaria
    if not active:
        logger.info("hub_token_refused", tenant_id=str(tenant_id), status=ent.status)
    return HubTokenVerifyOut(active=active, tenant_id=tenant_id)


@router.get(
    "/tenants/{tenant_id}/entitlements",
    response_model=InternalEntitlementOut,
    summary="Entitlement summary for a tenant (internal)",
    responses=_INTERNAL_RESPONSES,
)
async def internal_entitlements(
    tenant_id: Annotated[UUID, Path(description="Tenant UUID — the only scope.")],
    session: AsyncSession = Depends(get_session),
) -> InternalEntitlementOut:
    """The entitlement summary secretarIA's plugin registry gates on (PROMPT 3 seam).

    Same resolution as `GET /entitlements` (coherent defaults when no row; catalog-
    normalized addons/limits) minus the browser-facing fields.
    """
    ent = await resolve_entitlement(session, tenant_id)
    return InternalEntitlementOut(
        tenant_id=tenant_id,
        status=ent.status,
        active=ent.status in ACTIVE_STATUSES,
        secretaria_enabled=ent.products.secretaria,
        plan=ent.plan,
        secretaria_tier=ent.secretaria_tier,
        addons=ent.addons,
        limits=ent.limits,
    )


@router.post(
    "/usage-events",
    response_model=UsageEventOut,
    summary="Record one usage event (internal, metering leg)",
    responses=_INTERNAL_RESPONSES,
)
async def create_usage_event(
    payload: UsageEventIn,
    session: AsyncSession = Depends(get_session),
) -> UsageEventOut:
    """The inbound metering write path (stripe-billing-entitlements: METERING leg only —
    no Stripe call here, see `services/usage.py`'s TODO for the future meter forward).

    Idempotent on `payload.event_id` (the caller's own key, e.g.
    "reminder:24h:<appointment_id>"): a replayed event is `200 {"recorded": false}`, not
    an error, and does NOT double-increment `entitlements.usage`. `feature` is validated
    against the catalog's `LIMIT_KEYS` at the schema layer (422 on an unknown feature).
    """
    recorded = await record_usage(session, payload)
    return UsageEventOut(recorded=recorded)


@router.post(
    "/precheck-handoff",
    response_model=PrecheckHandoffOut,
    summary="Hand off a patient session to PreCheck (internal)",
    responses={
        **_INTERNAL_RESPONSES,
        403: {"description": "Tenant not entitled to PreCheck (inactive status or precheck disabled)."},
        404: {"description": "No PreCheck clinic mapped to this tenant."},
        409: {"description": "Patient already has a conflicting active PreCheck session."},
        502: {"description": "PreCheck upstream error / network failure (generic detail, never the upstream body)."},
        503: {"description": "PRECHECK_BASE_URL/PRECHECK_INTERNAL_TOKEN not configured, or PreCheck itself degraded."},
    },
)
async def precheck_handoff(
    payload: PrecheckHandoffIn,
    session: AsyncSession = Depends(get_session),
) -> PrecheckHandoffOut:
    """secretarIA -> brain-api -> PreCheck (CONTRACTS.md §12.3): pre-seed a WhatsApp
    patient session in PreCheck ahead of any keyword matching, one leg of three.

    brain-api is the entitlement AUTHORITY here — PreCheck is never asked to re-check
    it. Not active/trialing OR `precheck_enabled=false` -> `403 precheck_not_entitled`,
    fail closed, BEFORE any upstream call. Otherwise the outbound leg
    (`services/precheck_handoff.request_handoff`) forwards to PreCheck and maps its
    response 1:1 (see that module's docstring for the full status matrix). No DB write
    on this path; nothing is cached or retried here.
    """
    ent = await resolve_entitlement(session, payload.tenant_id)
    entitled = ent.status in ACTIVE_STATUSES and ent.products.precheck
    if not entitled:
        logger.info(
            "precheck_handoff_not_entitled",
            tenant_id=str(payload.tenant_id),
            status=ent.status,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "precheck_not_entitled")

    result = await request_handoff(payload.tenant_id, payload.phone_number)
    return PrecheckHandoffOut(status=result["status"])
