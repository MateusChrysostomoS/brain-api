"""brain-api's INBOUND service-to-service API for the Portal (Brain-Message channel) —
the `/internal/brain-message/pending-*` routes — INTERNAL ONLY.

Split out of `api/internal.py` (purely mechanical, 2026-09-21): same trust boundary, same
shared secret PAIR as the rest of `/internal/*` — the caller sends `X-Internal-Api-Key` =
its `INTERNAL_API_KEY`, which MUST equal our `SECRETARIA_API_KEY` byte-for-byte — one
secret, both directions (CONTRACTS.md §12.1).

Gate semantics mirror secretaria's `require_internal_api_key`: fail CLOSED when our
key is unset (403), 401 on mismatch, constant-time compare, the candidate key is never
logged. During a rotation window `SECRETARIA_API_KEY_PREVIOUS` is also accepted
(verification only — docs/key-rotation.md).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.internal import require_internal_api_key
from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.email_mask import mask_email
from brain_api.core.logging import get_logger
from brain_api.core.ratelimit import SlidingWindowLimiter
from brain_api.models.patient_access import MessagePatient
from brain_api.schemas.portal.internal import (
    PatientNameIn,
    PatientNameOut,
    PendingEmailClaimIn,
    PendingEmailClaimOut,
    PendingIdentityIn,
    PendingIdentityStatusOut,
    PendingOtpCancelOut,
    PendingOtpRequestOut,
    PendingOtpVerifyIn,
    PendingOtpVerifyOut,
)
from brain_api.services import secretaria_provisioning
from brain_api.services.portal import patient_access

logger = get_logger(__name__)

_pending_otp_request_limiter = SlidingWindowLimiter(
    "internal_pending_otp_request",
    lambda: get_settings().PATIENT_OTP_EMAIL_RATE_LIMIT_PER_MIN,
)
_pending_otp_verify_limiter = SlidingWindowLimiter(
    "internal_pending_otp_verify",
    lambda: get_settings().PATIENT_VERIFY_RATE_LIMIT_PER_MIN,
)
_OTP_EMAIL_TEMPLATE = "patient_access_otp"

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_internal_api_key)],
)

_INTERNAL_RESPONSES = {
    401: {"description": "Missing or invalid X-Internal-Api-Key."},
    403: {"description": "SECRETARIA_API_KEY not configured on the server."},
}


@router.post(
    "/brain-message/pending-email",
    response_model=PendingEmailClaimOut,
    summary="Record the e-mail a pending conversation captured (internal)",
    responses={
        **_INTERNAL_RESPONSES,
        404: {
            "description": (
                "Not a live pending visit — one answer for an unknown handle, the wrong "
                "clinic, an expired visit and one already turned into an account."
            )
        },
    },
)
async def claim_pending_email(
    payload: PendingEmailClaimIn,
    session: AsyncSession = Depends(get_session),
) -> PendingEmailClaimOut:
    """secretarIA -> brain-api: the visitor typed an address into the chat.

    The address is written on the VISIT (`message_pending_sessions.email`), never on the
    identity and never on an account: it is claimed, not proven. The code that proves it is
    requested by the browser afterwards, and that route takes no address — it reads this one.
    That split is the whole point of doing this on the service leg (schemas/internal.py).

    `tenant_id` and `external_id` must BOTH match the visit, so a clinic's key cannot move an
    address onto another clinic's conversation.
    """
    claim = await patient_access.claim_pending_email(
        session, payload.tenant_id, payload.external_id, payload.email
    )
    if claim is None:
        logger.info("pending_email_claim_not_found", tenant_id=str(payload.tenant_id))
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pending_session_not_found")
    # The clinic only — never the address, never the handle (see the module's PII note).
    # `account_exists` names no inbox, so it may ride along; the mask may not be logged.
    logger.info(
        "pending_email_claimed",
        tenant_id=str(payload.tenant_id),
        account_exists=claim.account_exists,
    )
    # Both extra fields come STRAIGHT FROM THE SERVICE: the mask is built next to the address
    # it describes (`patient_access.PendingEmailClaim`) precisely so this layer never holds a
    # raw one to mask. `email_masked` is `None` unless an account was found — there is no
    # inbox to help anybody recognise when nobody is being asked to log back in.
    return PendingEmailClaimOut(
        status="claimed",
        account_exists=claim.account_exists,
        email_masked=claim.email_masked,
    )


@router.post(
    "/brain-message/pending-identity",
    response_model=PendingIdentityStatusOut,
    summary="Read the PII-free identity state of one Brain-Message conversation",
    responses=_INTERNAL_RESPONSES,
)
async def pending_identity_status(
    payload: PendingIdentityIn,
    session: AsyncSession = Depends(get_session),
) -> PendingIdentityStatusOut:
    """Tell secretarIA whether this exact clinic handle still needs an address.

    The answer deliberately carries no address, account id or token. A verified legacy
    identity (address present even if account adoption is waiting for its old cookie) also
    counts as verified: that patient already proved the inbox and must not be asked again.
    """
    patient = await session.get(MessagePatient, payload.external_id)
    if patient is None or patient.tenant_id != payload.tenant_id:
        return PendingIdentityStatusOut(status="unknown")
    pending = await patient_access.find_pending_identity(
        session, payload.tenant_id, payload.external_id
    )
    if pending is not None:
        if pending.verified_at is not None:
            return PendingIdentityStatusOut(status="verified")
        return PendingIdentityStatusOut(
            status="pending_claimed" if pending.email is not None else "pending_unclaimed"
        )
    if patient.email is not None:
        return PendingIdentityStatusOut(status="verified")
    return PendingIdentityStatusOut(status="unknown")


@router.post(
    "/brain-message/pending-otp/request",
    response_model=PendingOtpRequestOut,
    summary="Send the pending visit's post-booking code (internal)",
    responses={
        **_INTERNAL_RESPONSES,
        404: {"description": "No accessible visit for this clinic handle."},
        409: {"description": "The conversation has not captured an e-mail."},
        429: {"description": "Rate limited for this visit."},
        503: {"description": "The notification service did not accept the e-mail."},
    },
)
async def request_pending_otp_internal(
    payload: PendingIdentityIn,
    session: AsyncSession = Depends(get_session),
) -> PendingOtpRequestOut:
    """Service leg used by the post-booking hook; takes no address by design.

    Answers with the address MASKED (TASK-003 §3) so the card secretarIA renders can name the
    inbox the code went to. The raw value stays here: it is read from the visit row, handed to
    the notification leg, and the only form of it that leaves this function is
    `mask_email`'s. Nothing below logs it either — the lines here carry `tenant_id` only.
    """
    pending = await patient_access.find_pending_identity(
        session, payload.tenant_id, payload.external_id
    )
    if pending is None or pending.verified_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pending_session_not_found")
    if pending.email is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "pending_email_missing")
    if not _pending_otp_request_limiter.allow(str(pending.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")

    code = await patient_access.issue_pending_otp(session, pending)
    if code is None:  # state changed between lookup and issue
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pending_session_not_found")
    queued = await secretaria_provisioning.send_notification_email(
        pending.email,
        _OTP_EMAIL_TEMPLATE,
        {"code": code, "ttl_minutes": get_settings().PATIENT_OTP_EXPIRE_MINUTES},
    )
    if not queued:
        # Do not tell secretarIA "sent" (and make it promise a code in chat)
        # when the notification leg refused the e-mail. The account-wide OTP
        # may remain until its short TTL, but this visit must not advertise
        # code mode; a retry will issue and mark a fresh challenge.
        pending.otp_requested_at = None
        await session.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "notification_unavailable")
    logger.info("patient_pending_otp_requested_internal", tenant_id=str(payload.tenant_id))
    return PendingOtpRequestOut(status="sent", email_masked=mask_email(pending.email))


@router.post(
    "/brain-message/pending-otp/cancel",
    response_model=PendingOtpCancelOut,
    summary="Forget this visit's active code wait (internal)",
    responses={
        **_INTERNAL_RESPONSES,
        404: {"description": "No accessible visit for this clinic handle."},
    },
)
async def cancel_pending_otp_internal(
    payload: PendingIdentityIn,
    session: AsyncSession = Depends(get_session),
) -> PendingOtpCancelOut:
    """Called when the chat abandons the code wait to ask for a different address instead.

    Clears only the WAIT (`otp_requested_at`), never the claimed address — see
    `services/portal/patient_access.py::cancel_pending_otp` for why. A visit that is unknown,
    dead or wrong-clinic is the 404 every other route on this boundary uses; a live visit with
    nothing to cancel is still a 200 (`nothing_to_cancel`), because the caller already knows
    the visit exists.
    """
    pending = await patient_access.find_pending_identity(
        session, payload.tenant_id, payload.external_id
    )
    if pending is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pending_session_not_found")
    cancelled = await patient_access.cancel_pending_otp(
        session, payload.tenant_id, payload.external_id
    )
    return PendingOtpCancelOut(status="cancelled" if cancelled else "nothing_to_cancel")


@router.post(
    "/brain-message/pending-otp/verify",
    response_model=PendingOtpVerifyOut,
    summary="Verify the code typed in the Brain-Message conversation (internal)",
    responses={
        **_INTERNAL_RESPONSES,
        400: {"description": "Wrong, expired, used or exhausted code."},
        404: {"description": "No accessible visit for this clinic handle."},
        409: {"description": "The conversation has not captured an e-mail."},
        429: {"description": "Rate limited for this visit."},
    },
)
async def verify_pending_otp_internal(
    payload: PendingOtpVerifyIn,
    session: AsyncSession = Depends(get_session),
) -> PendingOtpVerifyOut:
    """Prove the address without attempting to mint a browser credential on this leg.

    A successful retry is idempotent while the browser has not completed the exchange. The
    visitor's pending bearer remains readable until `/patient-access/pending/complete` sets
    the HttpOnly account cookie and returns the ordinary account tokens.
    """
    pending = await patient_access.find_pending_identity(
        session, payload.tenant_id, payload.external_id
    )
    if pending is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pending_session_not_found")
    if pending.verified_at is not None:
        return await _verified_out(session, pending.email)
    if pending.email is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "pending_email_missing")
    if not _pending_otp_verify_limiter.allow(str(pending.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    address = pending.email
    verified = await patient_access.verify_pending_identity(session, pending, payload.code)
    if verified is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_or_expired_code")
    return await _verified_out(session, address)


async def _verified_out(session: AsyncSession, address: str | None) -> PendingOtpVerifyOut:
    """`verified`, plus the proven address's account name when it has one.

    Only ever reached AFTER the code proved `address` (or on the idempotent retry of a visit
    already proven) — the proof that joins the visit to that account at `/pending/complete`
    is the same one that lets this clinic learn the name now. The name is PII: not logged.
    """
    name = await patient_access.account_name_for_proven_email(session, address) if address else None
    return PendingOtpVerifyOut(status="verified", patient_name=name)


@router.post(
    "/brain-message/patient-name",
    response_model=PatientNameOut,
    summary="Record the name a Brain-Message conversation captured (internal)",
    responses={
        **_INTERNAL_RESPONSES,
        404: {"description": "No identity with this handle at this clinic."},
    },
)
async def record_patient_name(
    payload: PatientNameIn,
    session: AsyncSession = Depends(get_session),
) -> PatientNameOut:
    """secretarIA -> brain-api: the patient answered "qual é o seu nome?" at this clinic.

    Kept on THIS clinic's identity; the account's next clinic receives it on its `open`
    (`services/patient_access.py::add_clinic` -> `account_display_name`), so the patient is
    not asked again (owner, 2026-09-24). `tenant_id` and `external_id` must both match the
    identity. The name is PII: the log line carries the clinic only.
    """
    saved = await patient_access.set_identity_name(
        session, payload.tenant_id, payload.external_id, payload.name.strip()
    )
    if not saved:
        logger.info("patient_name_identity_not_found", tenant_id=str(payload.tenant_id))
        raise HTTPException(status.HTTP_404_NOT_FOUND, "patient_not_found")
    logger.info("patient_name_recorded", tenant_id=str(payload.tenant_id))
    return PatientNameOut(status="saved")
