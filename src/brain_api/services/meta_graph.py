"""Meta Graph API client — the WhatsApp Embedded Signup authorization-code exchange.

`POST /doctor/onboarding/attempts` (api/onboarding.py) calls this to turn the `code` the
frontend's Meta JS SDK Embedded Signup flow hands back into a real access token, BEFORE
handing it to secretaria's whatsapp-connection endpoint. A failure here is never a hard
error to the caller: it is folded into a normal `signup_attempts` 'fail' row with
`error_code="token_exchange_failed"` (CONTRACT_onboarding_v1.md §7) — this module
therefore never raises, only returns `None` on any failure. `META_APP_SECRET` (a real
secret) is sent only as a query parameter on this one outbound call and is never logged
(structlog `redact_secrets` also blanks it defensively via the `client_secret` key hint).
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
