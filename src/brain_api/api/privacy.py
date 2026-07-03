"""LGPD cross-database privacy orchestration (CONTRACTS.md §14) — role `admin`.

EVERY route here is gated by `require_role("admin")` at the router level (mirrors
`api/admin.py`): a non-admin brain JWT gets `403` before any handler runs. There is no
tenant scoping — a platform admin acts on any subject in any tenant, which is exactly
the LGPD data-subject-request use case (a request can come in about any patient/user in
the mesh).

Both routes share the SAME double guard as the secretaria admin reset (`POST
/admin/secretaria/reset`, CONTRACTS §11.2): the router-level `admin` role gate AND an
explicit `confirm: true` body — a missing/false confirm is `400`, before any local or
upstream work happens (export is non-destructive but still requires confirmation, since
it touches PII across three services and should never fire from an accidental request).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, get_current_principal, require_role
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.schemas.privacy import PrivacyRequestIn, PrivacyResultOut
from brain_api.services import privacy as privacy_service

logger = get_logger(__name__)

# Router-level gate: all /admin/privacy/* require an `admin` role (403 otherwise).
router = APIRouter(prefix="/admin/privacy", dependencies=[Depends(require_role("admin"))])

_CONFIRM_DETAIL = "Set confirm: true to proceed."


@router.post(
    "/erasure",
    response_model=PrivacyResultOut,
    summary="Cross-database subject erasure (LGPD 'right to be forgotten')",
    responses={
        400: {"description": "confirm was not true."},
        409: {"description": "Target is a platform admin (cannot be erased this way)."},
        422: {"description": "Missing email (user) or wa_id+tenant_id (patient)."},
    },
)
async def erasure(
    payload: PrivacyRequestIn,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> PrivacyResultOut:
    """Erase a subject's data across brain + secretaria + precheck.

    Idempotent: re-running an erasure for an already-erased subject yields zero counts
    everywhere and still answers `200`. `status` is `"partial"` (not an HTTP error) if
    any upstream leg failed or is unconfigured — the caller retries; a retry only ever
    re-touches what is still there.
    """
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _CONFIRM_DETAIL)
    return PrivacyResultOut(**await privacy_service.erase_subject(session, principal, payload))


@router.post(
    "/export",
    response_model=PrivacyResultOut,
    summary="Cross-database subject export (LGPD data portability)",
    responses={
        400: {"description": "confirm was not true."},
        422: {"description": "Missing email (user) or wa_id+tenant_id (patient)."},
    },
)
async def export(
    payload: PrivacyRequestIn,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> PrivacyResultOut:
    """Collect a subject's data across brain + secretaria + precheck.

    Each service key carries that service's own bundle (or a `status` marker if that
    leg failed/is unconfigured/is inapplicable). The persisted audit row stores COUNTS
    of collected records only — never this response's data.
    """
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _CONFIRM_DETAIL)
    logger.info("admin_privacy_export", actor_user_id=principal.user_id)
    return PrivacyResultOut(**await privacy_service.export_subject(session, principal, payload))
