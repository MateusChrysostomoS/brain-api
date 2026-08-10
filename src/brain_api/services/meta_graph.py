"""Meta Graph API client — WhatsApp Embedded Signup token exchange + WABA webhook
subscription.

`POST /doctor/onboarding/attempts` (api/onboarding.py) calls `exchange_code_for_token` to
turn the `code` the frontend's Meta JS SDK Embedded Signup flow hands back into a real
access token, BEFORE handing it to secretaria's whatsapp-connection endpoint. A failure
here is never a hard error to the caller: it is folded into a normal `signup_attempts`
'fail' row with `error_code="token_exchange_failed"` (CONTRACT_onboarding_v1.md §7) — this
module therefore never raises, only returns `None`/`False` on any failure. `META_APP_SECRET`
(a real secret) is sent only as a query parameter on this one outbound call and is never
logged (structlog `redact_secrets` also blanks it defensively via the `client_secret` key
hint). The same call also then invokes `subscribe_app_to_waba` to subscribe this app to
the client's WABA webhooks (required for both the standard and the Coexistence flow) —
its `access_token` argument is sent ONLY as a Bearer header, never a query param, never
logged.
"""

from typing import Any

import httpx

from brain_api.config import get_settings
from brain_api.core.logging import get_logger

logger = get_logger(__name__)

_TOKEN_EXCHANGE_PATH = "/oauth/access_token"
# Not exposed as a setting (CONTRACT_onboarding_v1.md §12 does not list one for this
# call) — a short, fixed timeout for a simple GET, consistent with the other httpx
# service clients in this codebase.
_TIMEOUT_SECONDS = 10.0


async def exchange_code_for_token(code: str) -> str | None:
    """`GET {META_GRAPH_BASE_URL}/oauth/access_token` — returns the access token, or
    `None` when unconfigured (`META_APP_ID`/`META_APP_SECRET` unset), unreachable, a
    non-200 response, or a response missing `access_token`. Never raises.
    """
    settings = get_settings()
    if not settings.META_APP_ID or not settings.META_APP_SECRET:
        logger.warning("meta_token_exchange_unconfigured")
        return None
    try:
        async with httpx.AsyncClient(
            base_url=settings.META_GRAPH_BASE_URL, timeout=_TIMEOUT_SECONDS
        ) as client:
            resp = await client.get(
                _TOKEN_EXCHANGE_PATH,
                params={
                    "client_id": settings.META_APP_ID,
                    "client_secret": settings.META_APP_SECRET,
                    "code": code,
                },
            )
    except httpx.RequestError:
        logger.warning("meta_token_exchange_unreachable")
        return None

    if resp.status_code != 200:
        logger.warning("meta_token_exchange_failed", upstream_status=resp.status_code)
        return None

    body: Any = resp.json()
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        logger.warning("meta_token_exchange_malformed_response")
        return None
    return token


async def subscribe_app_to_waba(waba_id: str, access_token: str) -> bool:
    """`POST {META_GRAPH_BASE_URL}/{waba_id}/subscribed_apps` — subscribes this app to
    the client's WABA webhooks (Meta returns `{"success": true}` on a 200). Idempotent on
    Meta's side (re-subscribing is a no-op there), so this function never caches. A
    failure here is never a hard error to the caller: `api/onboarding.py::post_attempt`
    folds it into a normal `signup_attempts` 'fail' row with
    `error_code="waba_subscribe_failed"` — this function therefore never raises, only
    returns `False` on any failure. `access_token` is sent ONLY as a Bearer
    `Authorization` header, never a query param, never logged.
    """
    settings = get_settings()
    try:
        async with httpx.AsyncClient(
            base_url=settings.META_GRAPH_BASE_URL, timeout=_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(
                f"/{waba_id}/subscribed_apps",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.RequestError:
        logger.warning("meta_waba_subscribe_failed")
        return False

    if resp.status_code != 200:
        logger.warning("meta_waba_subscribe_failed", upstream_status=resp.status_code)
        return False
    return True
