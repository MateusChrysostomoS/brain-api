"""The switchboard — one patient session, two product backends, zero text classification.

WHICH PRODUCT A MESSAGE BELONGS TO IS NEVER INFERRED HERE. The client says it, in the URL
(`/patient-access/threads/{product}/messages`), because the patient already said it by
picking a tab. Reading the text to guess would be a second, silent router that disagrees
with the one the patient can see — and would send an anamnesis answer to the booking
agent the first time someone types "sim". The only decision this module makes is whether
the named product is one the CLINIC actually has, which is an entitlement read, not a
guess.

TWO KEYS, TWO HEADERS, AND THEY ARE NOT INTERCHANGEABLE.
  secretarIA  ->  `X-Internal-Api-Key`  =  SECRETARIA_API_KEY
  PreCheck    ->  `X-Internal-Token`    =  PRECHECK_INTERNAL_TOKEN
Both are SERVICE-PAIR secrets (`auth-jwt-multitenant`): they authenticate brain-api to a
sibling service and nothing else. They are used HERE, on the outbound hop, precisely
because this hop is service-to-service. They authenticate NO end user — the patient's own
request into brain-api was authenticated one layer up by a scoped session token, and that
token never travels onward. Anyone tempted to let a browser present one of these keys
should re-read `api/internal.py`: the same secret would then be a login for every tenant
at once.

FAIL CLOSED, BUT SAY WHICH KIND OF CLOSED. An unconfigured upstream and a refused product
are different facts for the patient: "this clinic does not offer that here" (403) must
never be spelled the same as "the service is down" (502/503), or a support call about a
missing tab becomes unanswerable.
"""

from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.schemas.entitlement import EntitlementOut

logger = get_logger(__name__)

PRODUCT_SECRETARIA = "secretaria"
PRODUCT_PRECHECK = "precheck"
#: Display order of the tabs the patient sees; also the iteration order of `/threads`.
PRODUCTS: tuple[str, ...] = (PRODUCT_SECRETARIA, PRODUCT_PRECHECK)

# secretarIA's inbound scheme (secretaria api/internal.py: APIKeyHeader X-Internal-Api-Key).
_SECRETARIA_KEY_HEADER = "X-Internal-Api-Key"
# PreCheck's inbound scheme (PreCheck app/core/deps.py: require_internal_api_token). A
# DIFFERENT header AND a different secret from secretarIA's — the two services were built
# independently and never converged on one name. Sending either key under the other
# header authenticates nothing.
_PRECHECK_TOKEN_HEADER = "X-Internal-Token"


def available_products(ent: EntitlementOut) -> list[str]:
    """The products this tenant may talk to on the Brain-Message channel, in tab order.

    TWO gates, both required, and they answer different questions:
      * `channels.brain_message` — is this clinic reachable on this CHANNEL at all? Off
        means no thread exists, however many products were bought. A patient holding a
        valid session for a clinic that has since switched the channel off sees nothing,
        which is the honest answer.
      * `products.{secretaria,precheck}` — did this clinic buy that PRODUCT?

    Collapsing them into one flag was tempting and is wrong: a clinic turning WhatsApp
    back on for a migration would silently lose its products, or a clinic without
    PreCheck would get a PreCheck tab. They are independent facts (see `ChannelsOut`).
    """
    if not ent.channels.brain_message:
        return []
    enabled = {
        PRODUCT_SECRETARIA: ent.products.secretaria,
        PRODUCT_PRECHECK: ent.products.precheck,
    }
    return [product for product in PRODUCTS if enabled[product]]


def require_product(ent: EntitlementOut, product: str) -> None:
    """403 unless `product` is one this tenant may reach right now.

    The SAME 403 for "no such product", "not bought" and "channel off": distinguishing
    them would let a patient session enumerate what a clinic pays for.
    """
    if product not in available_products(ent):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "product_unavailable")


def _upstream(product: str) -> tuple[str, dict[str, str]]:
    """`(base_url, headers)` for `product`, or 503 when its leg of the mesh is unset.

    503 and not 502: nothing failed upstream — this deployment simply has no address or
    no key for that service, which is an operator fact, not a patient-visible outage of
    the sibling. Neither secret is ever returned to a caller or logged; only WHICH one is
    missing is (the name, never the value).
    """
    settings = get_settings()
    if product == PRODUCT_SECRETARIA:
        base, key, header = (
            settings.SECRETARIA_BASE_URL,
            settings.SECRETARIA_API_KEY,
            _SECRETARIA_KEY_HEADER,
        )
        missing = "SECRETARIA_BASE_URL" if not base else "SECRETARIA_API_KEY"
    elif product == PRODUCT_PRECHECK:
        base, key, header = (
            settings.PRECHECK_BASE_URL,
            settings.PRECHECK_INTERNAL_TOKEN,
            _PRECHECK_TOKEN_HEADER,
        )
        missing = "PRECHECK_BASE_URL" if not base else "PRECHECK_INTERNAL_TOKEN"
    else:  # pragma: no cover - `require_product` rejects unknown products first.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "product_unavailable")

    if not base or not key:
        logger.warning("switchboard_unconfigured", product=product, reason=missing)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "product_channel_unconfigured"
        )
    return base, {header: key}


async def _call(
    product: str,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """One authenticated hop into a sibling service. Upstream bodies NEVER reach the patient.

    A patient is not an operator: secretarIA's and PreCheck's error bodies name flows,
    clinics and internal states, and forwarding them would leak the mesh's shape to
    anyone with an e-mail address. So every upstream failure collapses to one of two
    generic answers, and the detail goes to the log instead.

    The one status that is passed THROUGH in spirit is 503: PreCheck answers it for
    `stage_unsupported_on_channel` and `clinic_flow_not_configured`, which are real
    "try again / not available here" facts the client should show as such rather than as
    a hard error.
    """
    base, headers = _upstream(product)
    settings = get_settings()
    timeout = (
        settings.SECRETARIA_TIMEOUT_SECONDS
        if product == PRODUCT_SECRETARIA
        else settings.PRECHECK_TIMEOUT_SECONDS
    )
    try:
        async with httpx.AsyncClient(base_url=base, timeout=timeout) as client:
            resp = await client.request(method, path, headers=headers, json=json, params=params)
    except httpx.RequestError as exc:
        logger.warning(
            "switchboard_upstream_unreachable", product=product, error=type(exc).__name__
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "product_unreachable") from exc

    if resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        logger.warning("switchboard_upstream_degraded", product=product, path=path)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "product_temporarily_unavailable")
    if resp.status_code >= 400:
        # 401/403 here means the shared key is WRONG (both sides set, different values) —
        # an operator problem that must never surface as the patient's own 401.
        logger.error(
            "switchboard_upstream_error",
            product=product,
            path=path,
            status=resp.status_code,
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "product_error")
    try:
        return resp.json()
    except ValueError:  # pragma: no cover - a 2xx non-JSON body is a contract break.
        logger.error("switchboard_upstream_not_json", product=product, path=path)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "product_error") from None


async def send_message(
    product: str,
    *,
    tenant_id: UUID,
    patient_ref: str,
    text: str,
    patient_name: str | None = None,
) -> dict[str, Any]:
    """Relay one patient message to `product`'s inbound endpoint.

    The two contracts differ in shape AND in kind, and the difference is not cosmetic:

    * secretarIA takes `external_id` and answers 202 with `{"status": "queued"}` — the
      agent turn runs on its arq worker, so there is no reply to return. The client polls.
    * PreCheck takes `session_ref` and answers 200 with the whole next turn, synchronously
      (its `/agent/next-question` was always called that way).

    Both bodies are built field-by-field rather than by splatting a dict: PreCheck's
    request model is `extra="forbid"`, so one stray key — `dedupe_id`, say, which
    secretarIA accepts — is a 422 from a service that is working perfectly
    (`frozen-contract-migration`).

    `tenant_id` and `patient_ref` come from the validated session, never from the request
    body. That is the whole tenant-isolation argument for this hop: a patient cannot name
    a clinic or a patient handle, so there is nothing to tamper with.
    """
    if product == PRODUCT_SECRETARIA:
        body: dict[str, Any] = {
            "tenant_id": str(tenant_id),
            "external_id": patient_ref,
            "text": text,
        }
        if patient_name:
            body["patient_name"] = patient_name
        return await _call(product, "POST", "/internal/brain-message/inbound", json=body)

    body = {
        "tenant_id": str(tenant_id),
        "session_ref": patient_ref,
        "text": text,
    }
    if patient_name:
        body["patient_name"] = patient_name
    return await _call(product, "POST", "/internal/brain-message/inbound", json=body)


async def list_messages(
    product: str,
    *,
    tenant_id: UUID,
    patient_ref: str,
    since: str | None = None,
) -> dict[str, Any]:
    """Poll `product` for this patient's transcript.

    Polling, not WebSockets, on purpose for this first version: secretarIA answers 202 and
    finishes the turn on a worker, so SOMETHING has to ask again either way, and a poll is
    a plain GET through the same nginx proxy the rest of the mesh already goes through. A
    push channel (SSE or a socket) is a real future optimization — it would cut the idle
    request rate and the "reply appears up to one interval late" feel — and it is NOT a
    requirement of this round. Recorded here so it is a decision, not an oversight.

    secretarIA REQUIRES `tenant_id` as a query parameter ("an `external_id` alone is a
    bearer-like handle") — that requirement is the reason a guessed id is not a
    cross-tenant read over there. PreCheck's route takes only `session_ref`, scoping by
    the address derived from it; brain-api still resolves that ref from the session, so
    the scope holds on this side regardless.
    """
    if product == PRODUCT_SECRETARIA:
        params: dict[str, Any] = {"tenant_id": str(tenant_id)}
        if since:
            params["since"] = since
        return await _call(
            product,
            "GET",
            f"/internal/brain-message/conversations/{patient_ref}/messages",
            params=params,
        )

    params = {"since": since} if since else {}
    return await _call(
        product,
        "GET",
        f"/internal/brain-message/sessions/{patient_ref}/messages",
        params=params,
    )
