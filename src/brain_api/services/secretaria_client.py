"""Service-to-service admin client into secretaria (the cross-API admin connection).

brain-api is the identity authority and the only browser-facing service. secretaria is
internal-only and has NO user/role system — its sole privileged surface is `/admin/*`,
guarded by a shared secret (`X-Admin-Token` checked against secretaria's `ADMIN_TOKEN`;
see secretaria `api/admin.py: require_admin`). So a brain PLATFORM ADMIN cannot "log in"
to secretaria. Instead brain-api calls secretaria's admin routes on the admin's behalf,
presenting the shared `SECRETARIA_ADMIN_TOKEN`. The brain `admin` role is enforced on the
brain-api route (router-level `require_role("admin")`); this module only carries the
service credential and shapes the upstream call.

The admin token is NEVER logged (structlog redaction also blanks it defensively) and never
echoed to the caller. Fail closed: if the base URL or token is unset, raise 503 BEFORE any
network call — an unconfigured mesh must not silently appear to "work".
"""

from typing import Any

import httpx
from fastapi import HTTPException, status

from brain_api.config import get_settings
from brain_api.core.logging import get_logger

logger = get_logger(__name__)

# secretaria's admin scheme header (secretaria api/admin.py: APIKeyHeader name="X-Admin-Token").
_ADMIN_TOKEN_HEADER = "X-Admin-Token"


def _upstream_detail(resp: httpx.Response) -> str:
    """Extract a safe `detail` string from an upstream error response."""
    try:
        body = resp.json()
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            return body["detail"]
    except ValueError:
        pass
    return "secretaria upstream error"


async def _admin_request(method: str, path: str, json: dict[str, Any] | None = None) -> Any:
    """Call `method path` on secretaria's admin surface with the shared admin token.

    Surfaces an upstream 4xx (e.g. secretaria's own 403 for a bad token) to the caller
    unchanged; collapses upstream 5xx / network errors to 502. Returns parsed JSON.
    """
    settings = get_settings()
    base = settings.SECRETARIA_BASE_URL
    token = settings.SECRETARIA_ADMIN_TOKEN
    if not base:
        logger.warning("secretaria_admin_unconfigured", reason="base_url", path=path)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "secretaria_not_configured")
    if not token:
        logger.warning("secretaria_admin_unconfigured", reason="admin_token", path=path)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "secretaria_admin_not_configured"
        )

    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=settings.SECRETARIA_TIMEOUT_SECONDS
        ) as client:
            # Send only the admin credential; never copy the whole request env.
            resp = await client.request(
                method, path, headers={_ADMIN_TOKEN_HEADER: token}, json=json
            )
    except httpx.RequestError as exc:
        logger.warning("secretaria_admin_unreachable", path=path)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "secretaria unavailable") from exc

    if resp.status_code >= 400:
        logger.info("secretaria_admin_upstream_error", path=path, upstream_status=resp.status_code)
        raised = resp.status_code if resp.status_code < 500 else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(raised, _upstream_detail(resp))

    return resp.json()


async def list_tenants() -> Any:
    """secretaria clinic list + Google Calendar health — `GET /admin/tenants`."""
    return await _admin_request("GET", "/admin/tenants")


async def reset_data(*, include_tenants: bool) -> Any:
    """DESTRUCTIVE: wipe secretaria conversation data — `POST /admin/reset`.

    Always sends `confirm: true` (secretaria's own anti-curl guard). The brain-api route
    adds a SECOND, independent `confirm` check of its own before this is ever reached.
    """
    return await _admin_request(
        "POST", "/admin/reset", json={"confirm": True, "include_tenants": include_tenants}
    )
