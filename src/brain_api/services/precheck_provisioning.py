"""Internal service-to-service PROVISIONING client into PreCheck (`X-Internal-Token`).

The write-side counterpart of `precheck_client.py` (which only PROXIES doctor/admin READS
with the caller's brain JWT). Same shape as `secretaria_provisioning.py` — same
never-raise contract, same never-leak-upstream-body rule, same "unconfigured on either
side folds into None" behaviour — with one difference worth stating: PreCheck's internal
surface authenticates with **`X-Internal-Token`** (`app/core/deps.py::
require_internal_api_token`), not secretaria's `X-Internal-Api-Key`. The shared secret is
`PRECHECK_API_KEY`, which must equal PreCheck's own `INTERNAL_API_TOKEN`.

Every function returns `None` on failure instead of raising: a PreCheck outage must never
break the Stripe webhook (which would make Stripe redeliver forever) nor the doctor
portal. The caller (`onboarding_sync.ensure_precheck_provisioned`) simply leaves the
tenant unprovisioned so the next retry picks it up.
"""

from typing import Any
from uuid import UUID

import httpx

from brain_api.config import get_settings
from brain_api.core.logging import get_logger

logger = get_logger(__name__)

#: PreCheck's inbound scheme header — app/core/deps.py::require_internal_api_token.
#: NOT secretaria's `X-Internal-Api-Key`; the two services differ here.
_INTERNAL_TOKEN_HEADER = "X-Internal-Token"


async def _request(
    method: str, path: str, *, json: dict[str, Any] | None = None
) -> httpx.Response | None:
    """Call `method path` on PreCheck's internal surface with the shared pair key.

    Returns the raw response so the caller can tell meaningful statuses (200 = already
    provisioned, 201 = created, 409 = conflict) from a hard failure. Returns `None` —
    NEVER raises — when the mesh is unconfigured locally, PreCheck is unreachable, or it
    answers 401/503 (key mismatch / its own `INTERNAL_API_TOKEN` unset). Logged at WARNING
    with the path only: never the token, never the request body (it carries a password).
    """
    settings = get_settings()
    base, key = settings.PRECHECK_BASE_URL, settings.PRECHECK_API_KEY
    if not base or not key:
        logger.warning(
            "precheck_provisioning_unconfigured",
            path=path,
            reason="base_url" if not base else "precheck_api_key",
        )
        return None

    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=settings.PRECHECK_TIMEOUT_SECONDS
        ) as client:
            resp = await client.request(
                method, path, headers={_INTERNAL_TOKEN_HEADER: key}, json=json
            )
    except httpx.RequestError:
        logger.warning("precheck_provisioning_unreachable", path=path)
        return None

    if resp.status_code == 401:
        logger.warning("precheck_provisioning_key_mismatch", path=path)
        return None
    if resp.status_code == 503:
        logger.warning("precheck_provisioning_unconfigured_upstream", path=path)
        return None
    return resp


async def provision_clinic(
    tenant_id: UUID,
    *,
    template_slug: str,
    clinic_name: str,
    clinic_slug: str,
    trigger_phrase: str,
    doctor_name: str,
    doctor_email: str,
    doctor_password: str,
    doctor_phone: str | None = None,
) -> dict[str, Any] | None:
    """`POST /internal/provision` — create (or idempotently confirm) the PreCheck clinic
    for a brain tenant.

    Returns the parsed body on success — the caller needs `doctor_user_id` from it, which
    is what `precheck_account_links` stores and the SSO handoff mints as `sub`. Returns
    `None` on any failure; never raises.

    PreCheck answers **201** when it created the clinic and **200** when this
    `brain_tenant_id` already had one (a Stripe webhook redelivery, or our own lazy
    retry) — both are success and both carry `doctor_user_id`, so both are returned. A
    409 means a genuine collision (clinic slug / doctor email / trigger phrase already
    taken by an unrelated clinic) and is logged with the upstream's error code only.
    """
    resp = await _request(
        "POST",
        "/internal/provision",
        json={
            "brain_tenant_id": str(tenant_id),
            "template_slug": template_slug,
            "clinic_name": clinic_name,
            "clinic_slug": clinic_slug,
            "trigger_phrase": trigger_phrase,
            "doctor": {
                "name": doctor_name,
                "email": doctor_email,
                "password": doctor_password,
            },
            "doctor_phone": doctor_phone,
        },
    )
    if resp is None:
        return None

    if resp.status_code in (200, 201):
        try:
            return resp.json()
        except ValueError:
            logger.warning("precheck_provisioning_bad_json", status=resp.status_code)
            return None

    # Never log the body: it echoes the payload, which carries the doctor's password.
    detail = ""
    try:
        detail = str(resp.json().get("detail", ""))[:120]
    except Exception:  # noqa: BLE001 - a non-JSON error body is not worth a crash
        pass
    logger.warning(
        "precheck_provisioning_rejected", status=resp.status_code, detail=detail
    )
    return None
