"""Internal service-to-service DATA client into secretaria (`X-Internal-Api-Key`).

This is the read path behind the doctor portal's `/doctor/appointments` and
`/doctor/patients`: brain-api calls secretaria's INTERNAL-ONLY `/internal/*` surface over
the internal Docker network, authenticated by the shared `SECRETARIA_API_KEY` (sent as the
`X-Internal-Api-Key` header). It MUST equal secretaria's own `INTERNAL_API_KEY`.

Distinct from `secretaria_client.py` (the admin connection that sends `X-Admin-Token` /
`SECRETARIA_ADMIN_TOKEN` to secretaria's `/admin/*` — a different secret and surface), and
from the PreCheck proxy (`precheck_client.py`), which forwards the caller's *brain JWT*
rather than a service key. Here nothing from the caller is forwarded: secretaria has no
notion of the user; the tenant scope is carried in the URL path and the brain-api route
fills it from `principal.tenant_id` (never client input), so cross-tenant reads are impossible.

Fail closed + degrade gracefully (an UNCONFIGURED mesh degrades; a MISCONFIGURED one errors):
  * If `SECRETARIA_BASE_URL` or `SECRETARIA_API_KEY` is unset HERE (e.g. local dev), the list
    calls return an empty stub page rather than erroring — the portal still renders.
  * If secretaria's OWN key is unset, it answers `403` (its server-side key is unconfigured);
    we treat that the same as locally unconfigured and degrade to an empty page. This matches
    the contract: an unconfigured key on EITHER side yields an empty page, never a 500.
  * A genuinely failing upstream — network error, secretaria `5xx`, or a key MISMATCH (both
    sides set but different ⇒ secretaria `401`) — collapses to a clean `502`. secretaria's
    response body is never surfaced (no leak), and a key problem is never mis-reported as the
    *doctor's* own `401`.

The key is NEVER logged (structlog `redact_secrets` also blanks it defensively).
"""

from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from brain_api.config import get_settings
from brain_api.core.logging import get_logger

logger = get_logger(__name__)

# secretaria's inbound scheme header (secretaria api/internal.py: APIKeyHeader X-Internal-Api-Key).
_INTERNAL_KEY_HEADER = "X-Internal-Api-Key"


def _empty_page() -> dict[str, Any]:
    """Graceful fallback when no secretaria upstream / key is configured locally.

    Shape matches secretaria's `{"data": [...]}` envelope (with a `stub` marker), so the
    frontend renders an empty list identically whether or not the mesh is wired.
    """
    return {"data": [], "stub": True}


async def _get_list(path: str, *, params: dict[str, Any]) -> Any:
    """GET an internal list `path` with the shared key, or degrade to an empty page.

    Returns parsed JSON on success. secretaria's `403` (its own key unconfigured) degrades to
    an empty page (an unconfigured mesh, like the local-unset case). Everything else that
    fails — network error, `5xx`, or a key MISMATCH (`401`) — collapses to `502` with a
    generic detail, never leaking secretaria's body or mis-reporting a key problem as the
    caller's own `401`.
    """
    settings = get_settings()
    base = settings.SECRETARIA_BASE_URL
    key = settings.SECRETARIA_API_KEY
    # Fail closed locally: an unconfigured mesh degrades to an empty page, never a 500.
    if not base or not key:
        logger.warning(
            "secretaria_internal_unconfigured",
            path=path,
            reason="base_url" if not base else "secretaria_api_key",
        )
        return _empty_page()

    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=settings.SECRETARIA_TIMEOUT_SECONDS
        ) as client:
            # Send only the internal key; never copy the whole request env.
            resp = await client.get(path, headers={_INTERNAL_KEY_HEADER: key}, params=params)
    except httpx.RequestError as exc:
        logger.warning("secretaria_internal_unreachable", path=path)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "secretaria unavailable") from exc

    if resp.status_code == status.HTTP_403_FORBIDDEN:
        # secretaria answers 403 ONLY when ITS OWN INTERNAL_API_KEY is unset (server
        # unconfigured). Per the contract, an unconfigured key on either side degrades to an
        # unconfigured). Per the contract, an unconfigured key on either side degrades to an
        # empty page — not a 500/502. (A key MISMATCH is 401, handled as an error below.)
        logger.warning("secretaria_internal_unconfigured_upstream", path=path)
        return _empty_page()

    if resp.status_code >= 400:
        # 401 (key mismatch) and 5xx collapse to a clean 502. Never pass secretaria's status
        # or body through: a key/config problem must not surface as the doctor's own 401.
        logger.warning(
            "secretaria_internal_upstream_error", path=path, upstream_status=resp.status_code
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "secretaria upstream error")

    return resp.json()


async def list_appointments(tenant_id: UUID, *, skip: int, limit: int) -> Any:
    """Tenant appointments — `GET /internal/tenants/{tenant_id}/appointments`.

    `tenant_id` is the caller's own (server-resolved) tenant; secretaria scopes the query
    to it. Returns secretaria's `{"data": [...]}` verbatim, or an empty page if unconfigured.
    """
    return await _get_list(
        f"/internal/tenants/{tenant_id}/appointments",
        params={"limit": limit, "offset": skip},
    )


async def list_patients(tenant_id: UUID, *, skip: int, limit: int) -> Any:
    """Tenant patients — `GET /internal/tenants/{tenant_id}/patients` (see `list_appointments`)."""
    return await _get_list(
        f"/internal/tenants/{tenant_id}/patients",
        params={"limit": limit, "offset": skip},
    )
