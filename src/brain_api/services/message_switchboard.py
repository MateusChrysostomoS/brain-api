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

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from fastapi.concurrency import run_in_threadpool

from brain_api.config import get_settings
from brain_api.core import attachments
from brain_api.core.attachments import AttachmentKind, CheckedAttachment
from brain_api.core.logging import get_logger
from brain_api.schemas.entitlement import EntitlementOut

logger = get_logger(__name__)

PRODUCT_SECRETARIA = "secretaria"
PRODUCT_PRECHECK = "precheck"
#: Display order of the tabs the patient sees; also the iteration order of `/threads`.
PRODUCTS: tuple[str, ...] = (PRODUCT_SECRETARIA, PRODUCT_PRECHECK)
#: Products whose relay CODE carries a file (`send_attachment`, `open_media`, the transcript
#: reference). PreCheck is out of this round on purpose (z_prompts/PLANO_PORTAL_API_MVP.md,
#: decision 1): adapting it is adding it here plus its branch in those functions — the
#: validation in `core/attachments.py` is product-agnostic and does not change.
ATTACHMENT_PRODUCTS: frozenset[str] = frozenset({PRODUCT_SECRETARIA})
#: Products whose transcript carries a delivery state and that take a patient read mark
#: (`mark_read`). PreCheck is out of this round for the same reason as attachments
#: (z_prompts/PROMPT_BRAIN_MESSAGE_STATUS_ENTREGA_2_BRAIN_API.md, decision 1).
READ_RECEIPT_PRODUCTS: frozenset[str] = frozenset({PRODUCT_SECRETARIA})

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


def default_product(ent: EntitlementOut) -> str | None:
    """The thread a link that names NO product opens on — the first one offered, or None.

    `available_products` is already in tab order (`PRODUCTS`), so this is literally "the tab
    the portal lands on". Deriving it from that list instead of hardcoding `"secretaria"` is
    the point: a clinic that has only PreCheck lands on PreCheck, and the answer here cannot
    drift from the tabs the patient actually sees.
    """
    offered = available_products(ent)
    return offered[0] if offered else None


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
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "product_channel_unconfigured")
    return base, {header: key}


async def _call(
    product: str,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    content: AsyncIterator[bytes] | None = None,
    content_headers: dict[str, str] | None = None,
    timeout: float | None = None,
    refusals: frozenset[str] = frozenset(),
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

    `refusals` names the attachment refusals a caller lets through (`send_attachment`,
    `attachments.PRODUCT_REFUSALS`): an upstream 4xx whose `detail.code` is one of them, at
    the status this service's own table gives it, becomes that `AttachmentRefused` — this
    service's code and sentence, still never the upstream's body.
    """
    base, headers = _upstream(product)
    settings = get_settings()
    if timeout is None:
        timeout = (
            settings.SECRETARIA_TIMEOUT_SECONDS
            if product == PRODUCT_SECRETARIA
            else settings.PRECHECK_TIMEOUT_SECONDS
        )
    try:
        async with httpx.AsyncClient(base_url=base, timeout=timeout) as client:
            resp = await client.request(
                method,
                path,
                headers={**headers, **(content_headers or {})},
                json=json,
                params=params,
                content=content,
            )
    except httpx.RequestError as exc:
        logger.warning(
            "switchboard_upstream_unreachable", product=product, error=type(exc).__name__
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "product_unreachable") from exc

    code = _refusal_code(resp) if refusals and 400 <= resp.status_code < 500 else None
    # `.get`: a code a caller lists but the table lacks falls through to the opaque 502 below
    # instead of a KeyError — the table, not the caller, decides what a refusal is.
    table_status = attachments.REFUSALS.get(code, (None, ""))[0] if code in refusals else None
    if table_status == resp.status_code:
        logger.info(
            "switchboard_upstream_refused",
            product=product,
            path=path,
            status=resp.status_code,
            code=code,
        )
        raise attachments.AttachmentRefused(code)
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


def _refusal_code(resp: httpx.Response) -> str | None:
    """`detail.code` of an upstream error body shaped `{"detail": {"code": ...}}`, else None."""
    try:
        body = resp.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if isinstance(code, str) else None


async def send_message(
    product: str,
    *,
    tenant_id: UUID,
    patient_ref: str,
    text: str,
    patient_name: str | None = None,
    interactive_reply_id: str | None = None,
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

    `interactive_reply_id` (a tap on a reply button / list row) rides only on the
    secretarIA leg: it is the one product whose inbound contract knows the field, and
    sending it to PreCheck would be the exact stray-key 422 described above. PreCheck's
    questionnaire accepts the option's label as `text`, which the client sends either way.
    """
    if product == PRODUCT_SECRETARIA:
        body: dict[str, Any] = {
            "tenant_id": str(tenant_id),
            "external_id": patient_ref,
            "text": text,
        }
        if patient_name:
            body["patient_name"] = patient_name
        if interactive_reply_id:
            body["interactive_reply_id"] = interactive_reply_id
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

    Both GET routes carry the tenant determined by the authenticated patient
    session. PreCheck filters the bm: session by clinic as well as reference;
    references shared across clinics therefore cannot cross the boundary.

    DELIVERY STATE (2026-09-19). secretarIA's messages carry `status` (enviado | entregue |
    lido | falhou), `delivered_at`, `read_at` and `updated_at`, and they pass through here
    untouched: the state is derived ONCE, by secretarIA (`models/message.py::status_of`
    there), and a second derivation here could only disagree with it. For the same reason
    `since` stays opaque — on secretarIA it now means "CHANGED strictly after" (compared
    against `updated_at`), so one message can come back on several polls as its state moves,
    and the client upserts by `id`. This service never filters, dedupes or re-sorts rows.
    """
    if product == PRODUCT_SECRETARIA:
        params: dict[str, Any] = {"tenant_id": str(tenant_id)}
        if since:
            params["since"] = since
        payload = await _call(
            product,
            "GET",
            f"/internal/brain-message/conversations/{patient_ref}/messages",
            params=params,
        )
        return _project_attachments(product, payload)

    params = {"tenant_id": str(tenant_id)}
    if since:
        params["since"] = since
    return await _call(
        product,
        "GET",
        f"/internal/brain-message/sessions/{patient_ref}/messages",
        params=params,
    )


# --- Opening the conversation (2026-09-19): the automation speaks first --------------------

_OPEN_PATH = "/internal/brain-message/open"

#: Outcomes of `open_conversation`, for the log line and for tests. They are OBSERVATIONS,
#: never a decision the caller acts on: every one of them means "the patient's request is
#: finished either way". `queued` = secretarIA created the conversation and enqueued the
#: greeting (202); `exists` = there was already a conversation with a message, so nothing was
#: sent (200); `unconfigured` = this deployment has no secretarIA leg; `failed` = anything
#: else, including a 404 from a secretarIA that does not have the route yet.
OPEN_QUEUED = "queued"
OPEN_EXISTS = "exists"
OPEN_UNCONFIGURED = "unconfigured"
OPEN_FAILED = "failed"


async def open_conversation(
    *, tenant_id: UUID, patient_ref: str, patient_name: str | None = None
) -> str:
    """Ask secretarIA to open this patient's conversation and greet them — FIRE AND FORGET.

    THE ONE FUNCTION IN THIS MODULE THAT NEVER RAISES, and the module's "fail closed, loudly"
    rule is suspended here on purpose. Every other hop happens while a patient waits for its
    answer, so a failure has to become their error. This one has no patient waiting: it is a
    side effect scheduled AFTER the response to `POST /patient-access/pending` (or
    `POST /patient-access/clinics`) has already been written. There is nothing left to fail.
    Turning an unreachable secretarIA into an exception here could only mean one thing — a
    patient who opened a clinic's link gets an error instead of a conversation, because the
    clinic's *greeting* could not be sent. That trade is never worth making.

    NO INBOUND MESSAGE IS FABRICATED. This route exists precisely so the greeting does not
    need one: secretarIA's conversation is otherwise born only from a patient message
    (`workers/tasks.py::_get_or_create_conversation` there), and the alternative — relaying a
    synthetic "oi" the patient never typed — would put a bubble in their own transcript that
    they did not write. TASK-003 §2 forbids it, and so does the `/pending` docstring's
    "NOTHING IS PRE-CREATED UPSTREAM" note, which this replaces for secretarIA only.

    IDEMPOTENCE IS THE CALLEE'S, NOT OURS. secretarIA answers `200 {"status": "exists"}` when
    the conversation already has a message and only `202 {"status": "queued"}` when it is
    genuinely new (TASK-003 §2). That is why this may be called from two places without any
    state kept here, and why a retry is harmless: guessing locally whether a greeting is due
    would be a second, silently diverging copy of a decision secretarIA already owns.

    Returns one of the `OPEN_*` constants, for the caller's tests and this module's log —
    never for control flow. `patient_name` is PII and is forwarded but never logged; nor is
    the handle, for the reason `services/patient_access.py` gives (it would line one person's
    visits up across clinics in the log).
    """
    try:
        base, headers = _upstream(PRODUCT_SECRETARIA)
    except HTTPException:
        # `_upstream` raises the patient-facing 503 this path has nobody to show it to.
        logger.warning("brain_message_open_unconfigured", tenant_id=str(tenant_id))
        return OPEN_UNCONFIGURED

    body: dict[str, Any] = {
        "tenant_id": str(tenant_id),
        "external_id": patient_ref,
        "patient_name": patient_name,
    }
    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=get_settings().SECRETARIA_TIMEOUT_SECONDS
        ) as client:
            resp = await client.request("POST", _OPEN_PATH, headers=headers, json=body)
    except httpx.HTTPError as exc:
        # `HTTPError`, not `RequestError`: a background task swallowing its own failure must
        # not be the thing that lets a read/decode error escape into the server's task log.
        logger.warning(
            "brain_message_open_unreachable", tenant_id=str(tenant_id), error=type(exc).__name__
        )
        return OPEN_FAILED
    except Exception as exc:  # noqa: BLE001 - see below; the docstring's promise is absolute
        # The only blanket catch in this module, and it is load-bearing rather than lazy.
        # "Never raises" has to hold for EVERY exception, not the expected ones: `httpx.
        # InvalidURL` is not an `HTTPError` (it inherits straight from `Exception`), so an
        # operator typo in `SECRETARIA_BASE_URL` would otherwise escape a background task and
        # surface as "Exception in ASGI application" AFTER a perfectly good response — the
        # most confusing possible failure. `Exception`, not `BaseException`, so a
        # `CancelledError` during shutdown still propagates and the task is cancelled properly.
        logger.error(
            "brain_message_open_unexpected_error",
            tenant_id=str(tenant_id),
            error=type(exc).__name__,
        )
        return OPEN_FAILED

    if resp.status_code == status.HTTP_202_ACCEPTED:
        logger.info("brain_message_open_queued", tenant_id=str(tenant_id))
        return OPEN_QUEUED
    if resp.status_code == status.HTTP_200_OK:
        logger.info("brain_message_open_exists", tenant_id=str(tenant_id))
        return OPEN_EXISTS
    # 404 included, and expected during the rollout window: this service may go live before
    # secretarIA has the route. The greeting simply does not happen, which is exactly today's
    # behaviour — the patient can still write first (`brain-mesh-opaque-5xx-diagnosis`: the
    # status is logged so a missing route is not mistaken for a mesh outage).
    logger.warning(
        "brain_message_open_refused", tenant_id=str(tenant_id), upstream_status=resp.status_code
    )
    return OPEN_FAILED


# --- Read receipts (2026-09-19): the patient's "I have seen up to here" --------------------


async def mark_read(
    product: str,
    *,
    tenant_id: UUID,
    patient_ref: str,
    up_to_message_id: UUID | None = None,
    up_to: datetime | None = None,
) -> dict[str, Any]:
    """Tell `product` the patient has seen their conversation up to ONE cursor.

    `POST /internal/brain-message/messages/read`, answering `{"marked": int, "applied": bool}`,
    passed on as is. The body is built field by field because secretarIA's model is
    `extra="forbid"` (`frozen-contract-migration`), and its scope keys are the SESSION's — the
    same `tenant_id` + `external_id` pair as the poll, so a patient can mark only the
    conversation they could read.

    A product without read receipts (PreCheck, this round) is answered HERE with the "does not
    apply" secretarIA itself gives a WhatsApp patient: `applied: false`, nothing changed, no
    network call. So the portal may mark every thread it shows without knowing which products
    keep a state — an error for a background side effect would only be noise to the patient.
    """
    if product not in READ_RECEIPT_PRODUCTS:
        return {"marked": 0, "applied": False}
    body: dict[str, Any] = {"tenant_id": str(tenant_id), "external_id": patient_ref}
    if up_to_message_id is not None:
        body["up_to_message_id"] = str(up_to_message_id)
    if up_to is not None:
        body["up_to"] = up_to.isoformat()
    return await _call(product, "POST", "/internal/brain-message/messages/read", json=body)


# --- Attachments (2026-09-18): the multipart relay, the transcript reference, the stream ---


async def send_attachment(
    product: str,
    *,
    tenant_id: UUID,
    patient_ref: str,
    attachment: CheckedAttachment,
    file: BinaryIO,
    text: str | None = None,
    patient_name: str | None = None,
    interactive_reply_id: str | None = None,
) -> dict[str, Any]:
    """Relay one patient message WITH a file to `product`'s inbound, as `multipart/form-data`.

    The SAME internal route and the SAME field names as `send_message`'s JSON body — only the
    encoding changes, plus one file part named `file`. What that part says about the file is
    this service's finding, never the browser's: the content type is the one SNIFFED from the
    bytes and the name is the cleaned one (`core/attachments.py`). The file streams from
    Starlette's spool in chunks — never re-encoded (no base64), never held in memory whole.

    httpx encodes the body (it owns the boundary and the quoting of names), but it reads a
    file synchronously, and measures it first through `fileno()` — which rolls a spooled file
    over to disk. On the event loop, every one of those reads would stall every other request
    this service is serving. So the encoder is built, and then iterated, in a worker thread
    (`_off_the_loop`); only the network write stays on the loop.

    The product validates again on its side and must accept whatever this edge accepted (same
    constants). If it refuses anyway, that is drift between the two repos rather than a
    patient error, so it surfaces as the ordinary `product_error` 502 with the upstream status
    in this service's log (brain-mesh-opaque-5xx-diagnosis). The exception is what only the
    product can know — consent in THIS conversation, its daily quota — relayed as the
    patient's own 4xx (`attachments.PRODUCT_REFUSALS`).
    """
    if product not in ATTACHMENT_PRODUCTS:  # pragma: no cover - the router refuses first.
        raise attachments.AttachmentRefused(attachments.ATTACHMENT_UNSUPPORTED_FOR_PRODUCT)
    data = {"tenant_id": str(tenant_id), "external_id": patient_ref}
    if text:
        data["text"] = text
    if patient_name:
        data["patient_name"] = patient_name
    if interactive_reply_id:
        data["interactive_reply_id"] = interactive_reply_id
    # A relative URL on purpose: the request is only an ENCODER here, so it carries no Host;
    # its two headers below are the whole framing of the body it built.
    encoded = await run_in_threadpool(
        httpx.Request,
        "POST",
        _INBOUND_PATH,
        data=data,
        files={"file": (attachment.filename, file, attachment.kind.content_type)},
    )
    return await _call(
        product,
        "POST",
        _INBOUND_PATH,
        content=_off_the_loop(encoded.stream),
        content_headers={
            name: encoded.headers[name]
            for name in ("Content-Type", "Content-Length")
            if name in encoded.headers
        },
        timeout=get_settings().ATTACHMENT_UPSTREAM_TIMEOUT_SECONDS,
        refusals=attachments.PRODUCT_REFUSALS,
    )


_INBOUND_PATH = "/internal/brain-message/inbound"


async def _off_the_loop(stream: Iterable[bytes]) -> AsyncIterator[bytes]:
    """A SYNC byte stream, pulled one chunk per worker-thread hop — never on the event loop."""
    chunks = iter(stream)
    while (chunk := await run_in_threadpool(next, chunks, None)) is not None:
        yield chunk


def _project_attachments(product: str, payload: Any) -> Any:
    """Rewrite each transcript message's `attachment` into the only shape a browser may see.

    Everything else in the transcript passes through untouched (`RelayOut` says why). This
    one key is the exception because it is where storage could leak: the product keeps an
    object key, and could one day add a signed URL, and neither may reach the patient — the
    portal's CSP would block the URL, and a URL that works for whoever holds it is a
    credential. What leaves is a whitelist (type, size, name) plus `media_path`, the route
    that streams the file under the patient's own session. A malformed reference becomes
    null, with a log line, rather than a broken message.
    """
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return payload
    for item in items:
        if isinstance(item, dict) and item.get("attachment") is not None:
            item["attachment"] = _attachment_ref(product, item)
    return payload


def _attachment_ref(product: str, item: dict[str, Any]) -> dict[str, Any] | None:
    raw = item["attachment"] if isinstance(item["attachment"], dict) else {}
    message_id, content_type = item.get("id"), raw.get("content_type")
    kind = attachments.ALLOWED_KINDS.get(content_type) if isinstance(content_type, str) else None
    size, name = raw.get("size_bytes"), raw.get("filename")
    if (
        kind is None
        or type(size) is not int
        or not 0 < size <= attachments.MAX_ATTACHMENT_BYTES
        or not isinstance(message_id, str)
        or not attachments.is_media_id(message_id)
    ):
        logger.warning("switchboard_attachment_ref_invalid", product=product)
        return None
    return {
        "content_type": kind.content_type,
        "size_bytes": size,
        "filename": attachments.safe_filename(name if isinstance(name, str) else None, kind),
        "media_path": f"/patient-access/threads/{product}/media/{message_id}",
    }


_MEDIA_PATH = "/internal/brain-message/media/{message_id}"


@dataclass(frozen=True)
class MediaStream:
    """An attachment on its way from the product to the browser.

    The upstream connection is released twice over, and either is enough: `chunks` closes it
    when it ends however it ends (`_capped`), and the router also hands `aclose` to Starlette
    as the response's background task. `aclose` is idempotent, so the second is harmless.
    """

    kind: AttachmentKind
    content_length: int | None
    chunks: AsyncIterator[bytes]
    aclose: Callable[[], Awaitable[None]]


async def open_media(
    product: str, *, tenant_id: UUID, patient_ref: str, message_id: str
) -> MediaStream:
    """Open `product`'s stream of ONE attachment, scoped to this patient's own conversation.

    `GET /internal/brain-message/media/{message_id}?tenant_id=..&external_id=..` — both query
    values off the SESSION, exactly like the poll. The product answers 404 unless the message
    belongs to that conversation, and that 404 goes on as the same `attachment_not_found` a
    nonexistent id gets.

    Re-checked here, because the answer lands in a browser under this service's origin: the
    type must be an accepted kind (a `text/html` from a confused or compromised upstream is
    refused, never streamed), a declared length over the ceiling is refused, and the body is
    cut at the ceiling whatever the upstream claimed.
    """
    if product not in ATTACHMENT_PRODUCTS or not attachments.is_media_id(message_id):
        raise _media_not_found()
    base, headers = _upstream(product)
    client = httpx.AsyncClient(
        base_url=base, timeout=get_settings().ATTACHMENT_UPSTREAM_TIMEOUT_SECONDS
    )
    try:
        response = await client.send(
            client.build_request(
                "GET",
                _MEDIA_PATH.format(message_id=message_id),
                headers=headers,
                params={"tenant_id": str(tenant_id), "external_id": patient_ref},
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning(
            "switchboard_upstream_unreachable", product=product, error=type(exc).__name__
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "product_unreachable") from exc

    async def aclose() -> None:
        await response.aclose()
        await client.aclose()

    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    kind = attachments.ALLOWED_KINDS.get(media_type)
    length = _declared_length(response)
    oversized = length is not None and length > attachments.MAX_ATTACHMENT_BYTES
    if response.status_code != status.HTTP_200_OK or kind is None or oversized:
        await aclose()
        raise _media_failure(product, response.status_code, kind is not None, oversized)
    return MediaStream(
        kind=kind,
        content_length=length,
        chunks=_capped(product, response.aiter_bytes(), aclose),
        aclose=aclose,
    )


def _declared_length(response: httpx.Response) -> int | None:
    """The upstream's Content-Length when it describes the bytes sent on, else None.

    A body sent with a Content-Encoding is decoded on the way (`aiter_bytes`), so its declared
    length describes different bytes and is dropped.
    """
    raw = response.headers.get("content-length", "")
    if response.headers.get("content-encoding") or not raw.isdigit():
        return None
    return int(raw)


def _media_not_found() -> HTTPException:
    refused = attachments.AttachmentRefused(attachments.ATTACHMENT_NOT_FOUND)
    return HTTPException(refused.status_code, refused.detail)


def _media_failure(
    product: str, status_code: int, type_accepted: bool, oversized: bool
) -> HTTPException:
    """An upstream answer that will not be streamed, mapped the way `_call` maps its own."""
    if status_code == status.HTTP_404_NOT_FOUND:
        # Also what a product WITHOUT the route answers (deploy order) — hence the log line.
        logger.info("switchboard_media_not_found", product=product)
        return _media_not_found()
    if status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        logger.warning("switchboard_upstream_degraded", product=product, path=_MEDIA_PATH)
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "product_temporarily_unavailable")
    logger.error(
        "switchboard_upstream_error",
        product=product,
        path=_MEDIA_PATH,
        status=status_code,
        type_accepted=type_accepted,
        oversized=oversized,
    )
    return HTTPException(status.HTTP_502_BAD_GATEWAY, "product_error")


async def _capped(
    product: str, chunks: AsyncIterator[bytes], aclose: Callable[[], Awaitable[None]]
) -> AsyncIterator[bytes]:
    """`chunks`, cut at the attachment ceiling, releasing the upstream however it ends.

    The cut is for an upstream that lies about size. The `finally` is for a patient who goes
    away mid-download: Starlette runs a response's background task on that path only while
    the server negotiates ASGI HTTP spec < 2.4 (uvicorn 0.49 says 2.3), and this does not
    depend on it — an abandoned generator is finalized, and its `finally` runs, either way.
    """
    sent = 0
    try:
        async for chunk in chunks:
            sent += len(chunk)
            if sent > attachments.MAX_ATTACHMENT_BYTES:
                logger.error("switchboard_media_oversized", product=product)
                return
            yield chunk
    finally:
        await aclose()
