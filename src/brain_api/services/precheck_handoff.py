"""secretarIA -> brain-api -> PreCheck patient handoff (CONTRACTS.md §12.3, one leg of 3).

brain-api is the entitlement/identity authority: secretarIA calls our INBOUND
`/internal/precheck-handoff` (api/internal.py, gated by the §12.1/§12.2 PAIR key), we
verify the tenant's PreCheck entitlement ourselves (PreCheck is never asked to
re-check it), and — only if entitled — forward to PreCheck's OWN internal surface to
pre-seed the WhatsApp session. brain-api keeps NO new state: pure pass-through
orchestration, one outbound call, no DB write here.

Reuses the PreCheck service credential from `services/privacy.py`'s LGPD leg
(`PRECHECK_BASE_URL` + `PRECHECK_INTERNAL_TOKEN`, header `X-Internal-Token`) — same
mesh pairing, same httpx-async-client shape. Distinct upstream PATH though: PreCheck's
internal router for THIS contract is NOT under `/api/v1` (unlike the privacy leg) —
`POST {PRECHECK_BASE_URL}/internal/precheck-handoff` (PreCheck-side frozen contract).

A handoff must fail LOUDLY, never degrade to a stub: unlike the doctor-portal read
proxies (`secretaria_internal.py`), an unconfigured mesh here is `503`, not an empty
page — secretarIA needs to know the handoff did NOT happen.
"""

from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from brain_api.config import get_settings
from brain_api.core.logging import get_logger

logger = get_logger(__name__)

# PreCheck's inbound scheme header for this contract (X-Internal-Token, PRECHECK_INTERNAL_TOKEN
# — the SAME service credential services/privacy.py sends for the LGPD orchestration leg).
_PRECHECK_TOKEN_HEADER = "X-Internal-Token"
_PRECHECK_HANDOFF_PATH = "/internal/precheck-handoff"
# The Portal half of the same contract. Different PATH, same credential and same guard
# (PreCheck puts `require_internal_api_token` on every `/internal/*` route).
_PRECHECK_OPEN_PATH = "/internal/brain-message/open"

# PreCheck's two opening outcomes -> the two words this contract already speaks. A session
# that was at INIT and now holds the welcome is `seeded`; one that had already started is
# `already_active`. Mapped, not passed through: `PrecheckHandoffOut` is a frozen two-value
# Literal that secretarIA already branches on, and inventing a third word here would break
# our own response model rather than reach the patient.
_OPEN_OUTCOMES = {"awaiting_consent": "seeded", "exists": "already_active"}

# Generic, never-leak-upstream-body details for the collapsed failure cases.
_GENERIC_UNAVAILABLE = "precheck_handoff_unavailable"
_GENERIC_FAILED = "precheck_handoff_failed"


async def request_handoff(
    tenant_id: UUID,
    phone_number: str,
    *,
    patient_name: str | None = None,
    booked_service: str | None = None,
) -> dict[str, Any]:
    """POST PreCheck's `/internal/precheck-handoff`; caller has ALREADY confirmed the
    tenant's entitlement (this function does no entitlement check of its own).

    `patient_name`/`booked_service` are optional booking context (FEAT 38), forwarded
    verbatim and ONLY when not `None` — an omitted field is absent from the JSON, never
    an explicit `null`, so the payload a caller that doesn't set them produces stays
    byte-identical to the pre-FEAT-38 one. Keyword-only on purpose: two adjacent
    same-typed optionals are trivially swapped positionally, and swapping THESE two
    would put a patient's name into an operational field. Neither value is logged —
    `patient_name` is PII (§3 of the FEAT 38 brief) and `booked_service` has no
    operational reason to appear, so both stay out of every line below.

    Returns PreCheck's parsed 200 body (`{"status": "seeded" | "already_active"}`) on
    success. Every other outcome raises the mapped `HTTPException` directly, so the
    router stays a thin pass-through — see CONTRACTS.md §12.3 for the full matrix:

      * unconfigured (`PRECHECK_BASE_URL`/`PRECHECK_INTERNAL_TOKEN` empty) -> `503`
      * upstream `404` (no PreCheck clinic mapped to this tenant)          -> `404`
      * upstream `409` (patient already has a conflicting active session) -> `409`
      * upstream `503` (PreCheck itself degraded)                         -> `503`
      * upstream `422`/other `4xx`/`5xx`, or a network error/timeout      -> `502`

    The upstream response BODY is never surfaced in any error case (only our own
    generic detail strings) — mirrors `services/secretaria_internal.py`.
    """
    settings = get_settings()
    base, token = settings.PRECHECK_BASE_URL, settings.PRECHECK_INTERNAL_TOKEN
    if not base or not token:
        logger.warning("precheck_handoff_unconfigured", tenant_id=str(tenant_id))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "precheck_handoff_not_configured"
        )

    # Context keys are added only when present — see the docstring: absent, not null.
    payload: dict[str, Any] = {
        "brain_tenant_id": str(tenant_id),
        "phone_number": phone_number,
    }
    if patient_name is not None:
        payload["patient_name"] = patient_name
    if booked_service is not None:
        payload["booked_service"] = booked_service

    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=settings.PRECHECK_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(
                _PRECHECK_HANDOFF_PATH,
                headers={_PRECHECK_TOKEN_HEADER: token},
                json=payload,
            )
    except httpx.RequestError as exc:
        logger.warning("precheck_handoff_unreachable", tenant_id=str(tenant_id))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, _GENERIC_FAILED) from exc

    if resp.status_code == status.HTTP_200_OK:
        body = resp.json()
        logger.info(
            "precheck_handoff_ok", tenant_id=str(tenant_id), outcome=body.get("status")
        )
        return body

    if resp.status_code == status.HTTP_404_NOT_FOUND:
        logger.warning("precheck_handoff_no_clinic", tenant_id=str(tenant_id))
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no_clinic_for_tenant")

    if resp.status_code == status.HTTP_409_CONFLICT:
        logger.warning("precheck_handoff_conflict", tenant_id=str(tenant_id))
        raise HTTPException(status.HTTP_409_CONFLICT, "conflicting_active_session")

    if resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        logger.warning("precheck_handoff_upstream_unavailable", tenant_id=str(tenant_id))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _GENERIC_UNAVAILABLE)

    # 422 / any other 4xx / 5xx: collapse to a clean 502, upstream body never surfaced.
    logger.warning(
        "precheck_handoff_upstream_error",
        tenant_id=str(tenant_id),
        upstream_status=resp.status_code,
    )
    raise HTTPException(status.HTTP_502_BAD_GATEWAY, _GENERIC_FAILED)


async def request_portal_handoff(
    tenant_id: UUID,
    session_ref: str,
    *,
    patient_name: str | None = None,
) -> dict[str, Any]:
    """The same hand-off for a patient who has no phone number.

    A Portal patient is `wa_id IS NULL` by design, so `request_handoff` above cannot
    name them: its upstream route requires `phone_number` matching `^\\d{8,15}$`.
    This one addresses them by the handle the rest of the Brain-Message mesh already
    uses — `MessagePatient.id`, which is also PreCheck's `session_ref` — and calls
    PreCheck's opening route instead.

    Why not `/internal/brain-message/inbound` with an empty `text`, which the wire would
    have accepted: that is the INBOUND route, and its safety here would rest entirely on
    the session being at `INIT`. It is not, whenever the patient opened the pre-consult
    tab before booking — and there an empty text reaches PreCheck's `_turn()`, which asks
    the agent to classify a message nobody wrote and can SAVE an answer as if given.
    `/internal/brain-message/open` exists precisely so that decision is made by the side
    that owns the session state, and it writes nothing at all past `INIT`.

    SYNCHRONOUS, unlike `message_switchboard.open_conversation`'s fire-and-forget
    greeting, and the difference is not stylistic: secretarIA sends the pre-consult
    invitation ONLY on `seeded`/`already_active` (`plugins/precheck_handoff.py`), so a
    status invented before the upstream answered would invite a patient to a
    questionnaire that may not exist. The greeting has no such consumer.

    `booked_service` is deliberately NOT forwarded: PreCheck's opening route does not
    declare it and its request model is `extra="forbid"`, so sending it would be a `422`
    from a service that is working perfectly (`frozen-contract-migration`). The WhatsApp
    leg keeps sending it; closing that gap needs a change on PreCheck's side and is
    recorded in `tasks/TASK-004/TASK.md`, not papered over here.

    Same status matrix as `request_handoff` — deliberately, so secretarIA needs no second
    table — with one addition: an upstream `200` whose `status` is neither of the two
    known words is a `502`, never a guess.
    """
    settings = get_settings()
    base, token = settings.PRECHECK_BASE_URL, settings.PRECHECK_INTERNAL_TOKEN
    if not base or not token:
        logger.warning("precheck_portal_handoff_unconfigured", tenant_id=str(tenant_id))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "precheck_handoff_not_configured"
        )

    payload: dict[str, Any] = {"tenant_id": str(tenant_id), "session_ref": session_ref}
    if patient_name is not None:
        payload["patient_name"] = patient_name

    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=settings.PRECHECK_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(
                _PRECHECK_OPEN_PATH,
                headers={_PRECHECK_TOKEN_HEADER: token},
                json=payload,
            )
    except httpx.RequestError as exc:
        logger.warning("precheck_portal_handoff_unreachable", tenant_id=str(tenant_id))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, _GENERIC_FAILED) from exc

    if resp.status_code == status.HTTP_200_OK:
        try:
            upstream = resp.json().get("status")
        except ValueError:
            upstream = None
        outcome = _OPEN_OUTCOMES.get(upstream) if isinstance(upstream, str) else None
        if outcome is None:
            logger.error(
                "precheck_portal_handoff_unknown_outcome", tenant_id=str(tenant_id)
            )
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, _GENERIC_FAILED)
        logger.info(
            "precheck_portal_handoff_ok", tenant_id=str(tenant_id), outcome=outcome
        )
        return {"status": outcome}

    if resp.status_code == status.HTTP_404_NOT_FOUND:
        logger.warning("precheck_portal_handoff_no_clinic", tenant_id=str(tenant_id))
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no_clinic_for_tenant")

    if resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        logger.warning(
            "precheck_portal_handoff_upstream_unavailable", tenant_id=str(tenant_id)
        )
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _GENERIC_UNAVAILABLE)

    logger.warning(
        "precheck_portal_handoff_upstream_error",
        tenant_id=str(tenant_id),
        upstream_status=resp.status_code,
    )
    raise HTTPException(status.HTTP_502_BAD_GATEWAY, _GENERIC_FAILED)
