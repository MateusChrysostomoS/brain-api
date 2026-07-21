"""Internal service-to-service PROVISIONING client into secretaria (`X-Internal-Api-Key`).

Sibling of `services/secretaria_internal.py` (the doctor-portal READ path: appointments +
patients, which degrades every failure to an empty page). This module is the WRITE/config
side of the same `/internal/*` surface (CONTRACT_onboarding_v1.md §4): provisioning a
secretaria tenant row, connecting a WhatsApp number, pulling config-status, creating a
professional, activating a tenant, and enqueueing a transactional email. Same shared
secret (`SECRETARIA_API_KEY` sent as `X-Internal-Api-Key`, MUST equal secretaria's own
`INTERNAL_API_KEY`), same base-url/timeout settings, same never-leak-upstream-body
discipline as `secretaria_internal.py` and `secretaria_client.py`.

Every function here returns `None` / a tagged sentinel / `False` on failure instead of
raising an `HTTPException` — an unconfigured mesh, a network error, secretaria's own 403
(its key unconfigured), a key mismatch (401), and any 5xx are ALL "no answer this time"
to the caller, which decides for itself whether that is fail-soft (the provisioning
bridge, the config-status pull, activation, and notification emails are all best-effort
per CONTRACT_onboarding_v1.md scope A/B) or must surface as an error (the
professional-invite/self-bind routes map a `None` from `create_professional` to a clean
502 — those are foreground writes the doctor is waiting on). Nothing here raises itself,
unlike `secretaria_client.py`/`precheck_handoff.py`: the same operations are reused in
both fail-soft and fail-loud contexts depending on which endpoint calls them, so the
mapping has to live at the call site.

The key is NEVER logged (structlog `redact_secrets` also blanks it defensively).
"""

from typing import Any
from uuid import UUID

import httpx

from brain_api.config import get_settings
from brain_api.core.logging import get_logger

logger = get_logger(__name__)

# secretaria's inbound scheme header (secretaria api/internal.py: APIKeyHeader X-Internal-Api-Key).
_INTERNAL_KEY_HEADER = "X-Internal-Api-Key"

# connect_whatsapp() outcomes. A small, stable vocabulary the caller (api/onboarding.py)
# switches on to decide the resulting signup_attempts.result/error_code — deliberately
# NOT raw HTTP status codes, so the router logic reads as business outcomes.
CONNECTION_OK = "connected"
CONNECTION_CONFLICT = "conflict"
CONNECTION_ERROR = "error"


async def _request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response | None:
    """Call `method path` on secretaria's internal surface with the shared pair key.

    Returns the raw response so callers can distinguish meaningful status codes (404,
    409, ...) from a hard failure. Returns `None` — NEVER raises — when: the mesh is
    unconfigured locally (`SECRETARIA_BASE_URL`/`SECRETARIA_API_KEY` unset), secretaria
    is unreachable (network error/timeout), secretaria answers 403 (ITS OWN key is
    unconfigured — the same "unconfigured on either side" fold `secretaria_internal.py`
    uses), or 401 (a key MISMATCH — both sides set but different). Every case is logged
    at WARNING with the path only, never the key or the request body.
    """
    settings = get_settings()
    base, key = settings.SECRETARIA_BASE_URL, settings.SECRETARIA_API_KEY
    if not base or not key:
        logger.warning(
            "secretaria_provisioning_unconfigured",
            path=path,
            reason="base_url" if not base else "secretaria_api_key",
        )
        return None

    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=settings.SECRETARIA_TIMEOUT_SECONDS
        ) as client:
            resp = await client.request(
                method, path, headers={_INTERNAL_KEY_HEADER: key}, json=json, params=params
            )
    except httpx.RequestError:
        logger.warning("secretaria_provisioning_unreachable", path=path)
        return None

    if resp.status_code == 403:
        logger.warning("secretaria_provisioning_unconfigured_upstream", path=path)
        return None
    if resp.status_code == 401:
        logger.warning("secretaria_provisioning_key_mismatch", path=path)
        return None
    return resp


async def provision_tenant(
    tenant_id: UUID, *, clinic_name: str, contact_email: str | None, timezone: str
) -> bool:
    """`POST /internal/tenants` — create (or idempotently confirm) the secretaria tenant
    row for a brain tenant. Returns whether the call succeeded (2xx); never raises."""
    resp = await _request(
        "POST",
        "/internal/tenants",
        json={
            "tenant_id": str(tenant_id),
            "clinic_name": clinic_name,
            "contact_email": contact_email,
            "timezone": timezone,
        },
    )
    if resp is None:
        return False
    if resp.status_code >= 400:
        logger.warning(
            "secretaria_provision_tenant_failed",
            tenant_id=str(tenant_id),
            upstream_status=resp.status_code,
        )
        return False
    return True


async def connect_whatsapp(
    tenant_id: UUID, *, phone_number_id: str, waba_id: str | None, access_token: str | None
) -> str:
    """`POST /internal/tenants/{id}/whatsapp-connection`.

    Returns `CONNECTION_OK`, `CONNECTION_CONFLICT` (secretaria's 409
    `phone_number_conflict` — another tenant already holds this number), or
    `CONNECTION_ERROR` (anything else: unconfigured mesh, network error, 404 unknown
    tenant, 5xx). Never raises — the caller folds this into a `signup_attempts` outcome.
    """
    resp = await _request(
        "POST",
        f"/internal/tenants/{tenant_id}/whatsapp-connection",
        json={
            "phone_number_id": phone_number_id,
            "waba_id": waba_id,
            "access_token": access_token,
        },
    )
    if resp is None:
        return CONNECTION_ERROR
    if resp.status_code == 200:
        return CONNECTION_OK
    if resp.status_code == 409:
        return CONNECTION_CONFLICT
    logger.warning(
        "secretaria_connect_whatsapp_failed",
        tenant_id=str(tenant_id),
        upstream_status=resp.status_code,
    )
    return CONNECTION_ERROR


async def get_config_status(tenant_id: UUID) -> dict[str, Any] | None:
    """`GET /internal/tenants/{id}/config-status` — `None` on any failure (fail-soft;
    the caller simply skips this refresh cycle and tries again later)."""
    resp = await _request("GET", f"/internal/tenants/{tenant_id}/config-status")
    if resp is None or resp.status_code >= 400:
        return None
    return resp.json()


async def create_professional(
    tenant_id: UUID, *, name: str, specialty: str | None, about: str | None
) -> dict[str, Any] | None:
    """`POST /internal/tenants/{id}/professionals` (attach-by-name, idempotent on
    secretaria's side: a case-insensitive match on an existing active professional
    returns it with `created=false`). `None` on any failure — the caller (the
    invite/self-bind routes) maps that to a clean 502, since these are foreground writes
    the doctor is waiting on, unlike this module's other fail-soft callers."""
    resp = await _request(
        "POST",
        f"/internal/tenants/{tenant_id}/professionals",
        json={"name": name, "specialty": specialty, "about": about},
    )
    if resp is None or resp.status_code >= 400:
        return None
    return resp.json()


async def activate_tenant(tenant_id: UUID) -> dict[str, Any] | None:
    """`POST /internal/tenants/{id}/activate` — `None` on any failure (fail-soft; the
    ativo transition simply stays pending and is retried on the next config-status
    pull, `services/onboarding_sync.py::refresh_config_status`)."""
    resp = await _request("POST", f"/internal/tenants/{tenant_id}/activate", json={})
    if resp is None or resp.status_code >= 400:
        return None
    return resp.json()


async def send_notification_email(to: str, template: str, variables: dict[str, Any]) -> bool:
    """`POST /internal/notifications/email` — fire-and-forget. Returns whether it was
    queued; callers NEVER let a `False` block the operation that triggered the email
    (CONTRACT_onboarding_v1.md: the connection-success and professional-invite emails
    are both explicitly fail-soft — the invite response carries `invite_link` regardless
    so the owner can always share it manually)."""
    resp = await _request(
        "POST",
        "/internal/notifications/email",
        json={"to": to, "template": template, "variables": variables},
    )
    return resp is not None and resp.status_code < 400
