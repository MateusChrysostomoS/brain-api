"""BFF proxy into the PreCheck backend (RBAC task, Parts 2C/3B + 3C).

brain-api is the only browser-facing service; the portal never calls PreCheck directly
for these views. brain-api forwards the **caller's brain JWT** verbatim to PreCheck,
which validates it itself (PreCheck `app/core/brain_auth.py`) and scopes/role-gates the
result. So there is no second credential here — the same token that authorized the
brain-api route authorizes the upstream call.

The Authorization header is forwarded but NEVER logged (structlog redaction also blanks
it defensively). When `PRECHECK_BASE_URL` is unset (e.g. local dev without PreCheck), list
proxies degrade to an empty page rather than erroring, so the portal still renders.
"""

from typing import Any

import httpx
from fastapi import HTTPException, status

from brain_api.config import get_settings
from brain_api.core.logging import get_logger

logger = get_logger(__name__)


def _empty_page(skip: int, limit: int) -> dict[str, Any]:
    """The graceful fallback when no PreCheck upstream is configured."""
    return {"items": [], "total": 0, "skip": skip, "limit": limit, "stub": True}


def _upstream_detail(resp: httpx.Response) -> str:
    """Extract a safe `detail` string from an upstream error response."""
    try:
        body = resp.json()
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            return body["detail"]
    except ValueError:
        pass
    return "precheck upstream error"


async def _proxy_get(path: str, authorization: str, params: dict[str, Any] | None = None) -> Any:
    """GET `path` on the PreCheck backend, forwarding the caller's bearer token.

    Surfaces an upstream 4xx (e.g. PreCheck's own 403 for a non-admin) to the caller
    unchanged; collapses upstream 5xx / network errors to 502. Returns parsed JSON.
    """
    settings = get_settings()
    base = settings.PRECHECK_BASE_URL
    if not base:
        # Not configured: caller decides how to treat this (lists fall back to empty).
        logger.warning("precheck_proxy_unconfigured", path=path)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "precheck_not_configured")

    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=settings.PRECHECK_TIMEOUT_SECONDS
        ) as client:
            # Forward only the bearer credential; never copy the whole request env.
            resp = await client.get(path, headers={"Authorization": authorization}, params=params)
    except httpx.RequestError as exc:
        logger.warning("precheck_proxy_unreachable", path=path)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "precheck unavailable") from exc

    if resp.status_code >= 400:
        logger.info("precheck_proxy_upstream_error", path=path, upstream_status=resp.status_code)
        raised = resp.status_code if resp.status_code < 500 else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(raised, _upstream_detail(resp))

    return resp.json()


async def list_anamneses(authorization: str, skip: int, limit: int) -> Any:
    """Doctor anamneses list from PreCheck — `GET /api/v1/doctor/anamneses`."""
    if not get_settings().PRECHECK_BASE_URL:
        return _empty_page(skip, limit)
    return await _proxy_get(
        "/api/v1/doctor/anamneses", authorization, {"skip": skip, "limit": limit}
    )


async def get_anamnesis(authorization: str, anamnesis_id: int) -> Any:
    """Single anamnesis detail from PreCheck — `GET /api/v1/doctor/anamneses/{id}`."""
    return await _proxy_get(f"/api/v1/doctor/anamneses/{anamnesis_id}", authorization)


async def list_admin_anamneses(authorization: str, skip: int, limit: int) -> Any:
    """Admin anamneses list from PreCheck — `GET /api/v1/admin/anamneses` (cross-tenant).

    Returns an empty page if PreCheck is not configured locally (keeps the admin portal
    rendering); any configured-but-failing upstream still raises.
    """
    if not get_settings().PRECHECK_BASE_URL:
        return _empty_page(skip, limit)
    return await _proxy_get(
        "/api/v1/admin/anamneses", authorization, {"skip": skip, "limit": limit}
    )


async def get_admin_anamnesis(authorization: str, anamnesis_id: int) -> Any:
    """Single admin anamnesis detail from PreCheck — `GET /api/v1/admin/anamneses/{id}`."""
    return await _proxy_get(f"/api/v1/admin/anamneses/{anamnesis_id}", authorization)


async def get_admin_metrics(authorization: str, days: int, all_time: bool) -> Any:
    """Admin metrics overview from PreCheck — `GET /api/v1/admin/metrics`.

    When PreCheck is not configured locally, degrades to a stub payload (not a list, so
    `_empty_page`'s pagination shape does not apply) rather than erroring, so the portal
    still renders. `all_time` is forwarded as the `all` query param; httpx serializes a
    Python bool to the "true"/"false" strings the upstream FastAPI query parser expects.
    """
    if not get_settings().PRECHECK_BASE_URL:
        return {"stub": True}
    return await _proxy_get(
        "/api/v1/admin/metrics", authorization, {"days": days, "all": all_time}
    )
