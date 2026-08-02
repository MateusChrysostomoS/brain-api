"""brain-api's INBOUND service-to-service API for PreCheck (`/internal/precheck/*`) —
INTERNAL ONLY.

A SEPARATE service identity from `api/internal.py`'s secretarIA-facing surface: PreCheck
authenticates with its OWN shared secret pair (`X-Internal-Api-Key` checked against
`PRECHECK_API_KEY` / `PRECHECK_API_KEY_PREVIOUS`), never secretarIA's
`SECRETARIA_API_KEY` — each mesh caller gets its own identity (auth-jwt-multitenant
skill: "a single unauthenticated route — or a shared credential between two different
callers — is a direct path to impersonating a clinic"). Two routes: record a completed
PreCheck consultation (metering leg, `services/usage.py`) and read the tenant's current
quota decision (`services/precheck_billing.py`) — PreCheck composes the patient-facing
message itself from the numbers this returns; brain-api is the entitlement AUTHORITY,
never asked to render text.

Gate semantics mirror `api/internal.py`'s `require_internal_api_key` exactly: fail
CLOSED when `PRECHECK_API_KEY` is unset (403), 401 on mismatch, `secrets.compare_digest`,
the candidate key never logged. `PRECHECK_API_KEY_PREVIOUS` is accepted during a
rotation window (verification only — docs/key-rotation.md).
"""

import secrets
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.models import Entitlement
from brain_api.schemas.internal import UsageEventIn
from brain_api.schemas.internal_precheck import (
    PrecheckQuotaOut,
    PrecheckUsageEventIn,
    PrecheckUsageEventOut,
)
from brain_api.services import catalog, precheck_billing
from brain_api.services.usage import record_usage

logger = get_logger(__name__)

_precheck_key_scheme = APIKeyHeader(
    name="X-Internal-Api-Key",
    auto_error=False,
    description=(
        "Service-to-service shared secret (the brain<->PreCheck pair key, SEPARATE from "
        "secretarIA's). Configure via PRECHECK_API_KEY."
    ),
)


def require_precheck_api_key(
    key: Annotated[str | None, Security(_precheck_key_scheme)] = None,
) -> None:
    """Gate `/internal/precheck/*` on PreCheck's OWN shared pair key. Fail CLOSED; never
    log the candidate. Mirrors `api/internal.py::require_internal_api_key` exactly, but
    against `PRECHECK_API_KEY` — a DIFFERENT secret, a DIFFERENT service identity.
    """
    settings = get_settings()
    expected = settings.PRECHECK_API_KEY
    if not expected:
        logger.warning("precheck_internal_auth_unconfigured")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Internal API not configured.")
    accepted = [expected]
    if settings.PRECHECK_API_KEY_PREVIOUS:
        accepted.append(settings.PRECHECK_API_KEY_PREVIOUS)
    if not key or not any(secrets.compare_digest(key, candidate) for candidate in accepted):
        logger.warning("precheck_internal_auth_failed")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid internal API key.")


router = APIRouter(
    prefix="/internal/precheck",
    tags=["internal", "precheck"],
    dependencies=[Depends(require_precheck_api_key)],
)

_INTERNAL_RESPONSES = {
    401: {"description": "Missing or invalid X-Internal-Api-Key."},
    403: {"description": "PRECHECK_API_KEY not configured on the server."},
}


@router.post(
    "/usage-events",
    response_model=PrecheckUsageEventOut,
    summary="Record one completed PreCheck consultation (internal, metering leg)",
    responses={
        **_INTERNAL_RESPONSES,
        404: {"description": "Unknown tenant (no entitlement row)."},
    },
)
async def create_precheck_usage_event(
    payload: PrecheckUsageEventIn,
    session: AsyncSession = Depends(get_session),
) -> PrecheckUsageEventOut:
    """Recording is UNCONDITIONAL — an over-quota consultation still records (that IS the
    signal `GET /internal/precheck/quota/{tenant_id}` reports back); this endpoint never
    itself decides allow/deny. Idempotent on `payload.event_id`
    (`services/usage.py::record_usage`). 404 when the tenant carries no entitlement row
    at all (never provisioned) — unlike the general secretarIA-facing usage-events route,
    which upserts one, PreCheck usage always rides an already-provisioned PreCheck plan.
    """
    ent = await session.get(Entitlement, payload.tenant_id)
    if ent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entitlement_not_found")
    recorded = await record_usage(
        session,
        UsageEventIn(
            tenant_id=payload.tenant_id,
            feature=catalog.LIMIT_PRECHECK_CONSULTATIONS,
            amount=payload.amount,
            event_id=payload.event_id,
        ),
    )
    return PrecheckUsageEventOut(recorded=recorded)


@router.get(
    "/quota/{tenant_id}",
    response_model=PrecheckQuotaOut,
    summary="Current PreCheck quota decision for a tenant (internal)",
    responses={
        **_INTERNAL_RESPONSES,
        404: {"description": "Unknown tenant (no entitlement row)."},
    },
)
async def get_precheck_quota(
    tenant_id: Annotated[UUID, Path(description="Tenant UUID.")],
    session: AsyncSession = Depends(get_session),
) -> PrecheckQuotaOut:
    """The allow/deny DECISION plus the numbers behind it — no message field, PreCheck
    composes its own patient-facing text from `allowed`/`remaining`."""
    ent = await session.get(Entitlement, tenant_id)
    if ent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entitlement_not_found")
    summary = await precheck_billing.usage_summary(session, ent, datetime.now(UTC))
    return PrecheckQuotaOut(
        enforced=summary.enforced,
        allowed=summary.allowed,
        quota=summary.quota,
        used=summary.used,
        topup_credits=summary.topup_credits,
        remaining=summary.remaining,
    )
