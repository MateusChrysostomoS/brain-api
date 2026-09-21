"""brain-api's INBOUND service-to-service API (`/internal/*`) — INTERNAL ONLY.

The reverse direction of the existing brain-api -> secretaria data path: secretarIA
calls BACK into brain-api to (a) introspect a doctor-hub token and (b) read a tenant's
entitlement summary for its plugin gates. Same trust boundary, same shared secret PAIR:
the caller sends `X-Internal-Api-Key` = its `INTERNAL_API_KEY`, which MUST equal our
`SECRETARIA_API_KEY` byte-for-byte — one secret, both directions (CONTRACTS.md §12.1).

Gate semantics mirror secretaria's `require_internal_api_key`: fail CLOSED when our
key is unset (403), 401 on mismatch, constant-time compare, the candidate key is never
logged. During a rotation window `SECRETARIA_API_KEY_PREVIOUS` is also accepted
(verification only — docs/key-rotation.md).

No user JWT is accepted here and none is forwarded: the hub token is purpose-scoped
(`scope=secretaria_hub`) and the entitlement answer is recomputed live from the local
row (stripe-billing-entitlements: never from a token, never from Stripe).
"""

import secrets
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.email_mask import mask_email
from brain_api.core.logging import get_logger
from brain_api.core.ratelimit import SlidingWindowLimiter
from brain_api.core.security import decode_hub_token
from brain_api.models import Entitlement, Tenant, User
from brain_api.models.patient_access import MessagePatient
from brain_api.schemas.internal import (
    HubTokenVerifyIn,
    HubTokenVerifyOut,
    InternalEntitlementOut,
    InternalOnboardingEventIn,
    InternalOnboardingEventOut,
    InternalOnboardingListOut,
    InternalOnboardingTenantOut,
    InternalProfessionalEmailOut,
    InternalProfessionalEmailsOut,
    PendingEmailClaimIn,
    PendingEmailClaimOut,
    PendingIdentityIn,
    PendingIdentityStatusOut,
    PendingOtpRequestOut,
    PendingOtpVerifyIn,
    PendingOtpVerifyOut,
    PrecheckHandoffIn,
    PrecheckHandoffOut,
    UsageEventIn,
    UsageEventOut,
)
from brain_api.services import onboarding_sync, patient_access, secretaria_provisioning
from brain_api.services.entitlements import ACTIVE_STATUSES, resolve_entitlement
from brain_api.services.precheck_handoff import request_handoff, request_portal_handoff
from brain_api.services.usage import record_usage

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

_internal_key_scheme = APIKeyHeader(
    name="X-Internal-Api-Key",
    auto_error=False,
    description=(
        "Service-to-service shared secret (the brain<->secretaria pair key). "
        "Configure via SECRETARIA_API_KEY."
    ),
)


def require_internal_api_key(
    key: Annotated[str | None, Security(_internal_key_scheme)] = None,
) -> None:
    """Gate `/internal/*` on the shared pair key. Fail CLOSED; never log the candidate.

    Accepts the current key or, during a rotation window, the previous one — so the
    two services can be flipped one at a time with zero mesh downtime.
    """
    settings = get_settings()
    expected = settings.SECRETARIA_API_KEY
    if not expected:
        logger.warning("internal_auth_unconfigured")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Internal API not configured.")
    accepted = [expected]
    if settings.SECRETARIA_API_KEY_PREVIOUS:
        accepted.append(settings.SECRETARIA_API_KEY_PREVIOUS)
    if not key or not any(secrets.compare_digest(key, candidate) for candidate in accepted):
        logger.warning("internal_auth_failed")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid internal API key.")


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
    "/secretaria/hub-token/verify",
    response_model=HubTokenVerifyOut,
    summary="Introspect a secretarIA hub token (internal)",
    responses=_INTERNAL_RESPONSES,
)
async def verify_hub_token(
    payload: HubTokenVerifyIn,
    session: AsyncSession = Depends(get_session),
) -> HubTokenVerifyOut:
    """The LIVE subscription answer behind secretarIA's `verify_subscription_token`.

    `active=true` requires ALL of: a validly-signed, unexpired token with
    `scope=secretaria_hub` (a user JWT is rejected outright); entitlement status
    active/trialing; `secretaria_enabled`. Everything else is `active=false` — the
    caller fails closed. Always 200 for an authenticated service caller (the refusal
    is data, not an HTTP error).
    """
    claims = decode_hub_token(payload.token)
    if claims is None:
        return HubTokenVerifyOut(active=False)
    try:
        tenant_id = UUID(str(claims["sub"]))
    except ValueError:
        return HubTokenVerifyOut(active=False)

    # Parse-safe: a malformed/absent claim just reads as no professional scope, never a
    # 500 (same convention as api/deps.py's Principal parsing).
    professional_id: UUID | None = None
    raw_professional_id = claims.get("professional_id")
    if raw_professional_id:
        try:
            professional_id = UUID(str(raw_professional_id))
        except ValueError:
            professional_id = None

    ent = await resolve_entitlement(session, tenant_id)
    active = ent.status in ACTIVE_STATUSES and ent.products.secretaria
    if not active:
        logger.info("hub_token_refused", tenant_id=str(tenant_id), status=ent.status)
    return HubTokenVerifyOut(active=active, tenant_id=tenant_id, professional_id=professional_id)


@router.get(
    "/tenants/{tenant_id}/entitlements",
    response_model=InternalEntitlementOut,
    summary="Entitlement summary for a tenant (internal)",
    responses=_INTERNAL_RESPONSES,
)
async def internal_entitlements(
    tenant_id: Annotated[UUID, Path(description="Tenant UUID — the only scope.")],
    session: AsyncSession = Depends(get_session),
) -> InternalEntitlementOut:
    """The entitlement summary secretarIA's plugin registry gates on (PROMPT 3 seam).

    Same resolution as `GET /entitlements` (coherent defaults when no row; catalog-
    normalized addons/limits) minus the browser-facing fields.
    """
    ent = await resolve_entitlement(session, tenant_id)
    return InternalEntitlementOut(
        tenant_id=tenant_id,
        status=ent.status,
        active=ent.status in ACTIVE_STATUSES,
        secretaria_enabled=ent.products.secretaria,
        plan=ent.plan,
        secretaria_tier=ent.secretaria_tier,
        addons=ent.addons,
        limits=ent.limits,
    )


@router.get(
    "/tenants/{tenant_id}/professional-emails",
    response_model=InternalProfessionalEmailsOut,
    summary="Contact email per linked professional (internal)",
    responses=_INTERNAL_RESPONSES,
)
async def internal_professional_emails(
    tenant_id: Annotated[UUID, Path(description="Tenant UUID — the only scope.")],
    session: AsyncSession = Depends(get_session),
) -> InternalProfessionalEmailsOut:
    """Where to reach each of a tenant's professionals by email.

    brain-api is the single writer of identity, so a professional's address
    lives HERE (`users.email`, linked by `users.professional_id`) and nowhere
    else — secretarIA has no email column on `professionals` and deliberately
    will not get one, because a second copy would drift the moment a doctor
    changed their address. This is the read that lets secretarIA notify a
    professional about a new booking without holding that copy.

    Same join `GET /doctor/professionals` already does to fill
    `linked_user_email`, minus the secretaria-side roster proxy — the caller
    already knows its own roster and only needs the addresses.

    A professional with no linked user (created without an invite) is simply
    ABSENT from `items` rather than present with a null: the answer is "who can
    we reach", not "who exists". Users with no `professional_id` — a
    `secretary`, or an owner before linkage — are excluded by the same filter.
    """
    rows = (
        await session.execute(
            select(User.professional_id, User.email).where(
                User.tenant_id == tenant_id,
                User.professional_id.is_not(None),
                User.email.is_not(None),
            )
        )
    ).all()
    return InternalProfessionalEmailsOut(
        items=[
            InternalProfessionalEmailOut(professional_id=str(professional_id), email=email)
            for professional_id, email in rows
            if email
        ]
    )


@router.post(
    "/usage-events",
    response_model=UsageEventOut,
    summary="Record one usage event (internal, metering leg)",
    responses=_INTERNAL_RESPONSES,
)
async def create_usage_event(
    payload: UsageEventIn,
    session: AsyncSession = Depends(get_session),
) -> UsageEventOut:
    """The inbound metering write path (stripe-billing-entitlements: METERING leg only —
    no Stripe call here, see `services/usage.py`'s TODO for the future meter forward).

    Idempotent on `payload.event_id` (the caller's own key, e.g.
    "reminder:24h:<appointment_id>"): a replayed event is `200 {"recorded": false}`, not
    an error, and does NOT double-increment `entitlements.usage`. `feature` is validated
    against the catalog's `LIMIT_KEYS` at the schema layer (422 on an unknown feature).
    """
    recorded = await record_usage(session, payload)
    return UsageEventOut(recorded=recorded)


@router.post(
    "/precheck-handoff",
    response_model=PrecheckHandoffOut,
    summary="Hand off a patient session to PreCheck (internal)",
    responses={
        **_INTERNAL_RESPONSES,
        403: {
            "description": "Tenant not entitled to PreCheck (inactive status or precheck disabled)."
        },
        404: {"description": "No PreCheck clinic mapped to this tenant."},
        409: {"description": "Patient already has a conflicting active PreCheck session."},
        502: {
            "description": "PreCheck upstream error / network failure "
            "(generic detail, never the upstream body)."
        },
        503: {
            "description": "PRECHECK_BASE_URL/PRECHECK_INTERNAL_TOKEN not configured, "
            "or PreCheck itself degraded."
        },
    },
)
async def precheck_handoff(
    payload: PrecheckHandoffIn,
    session: AsyncSession = Depends(get_session),
) -> PrecheckHandoffOut:
    """secretarIA -> brain-api -> PreCheck (CONTRACTS.md §12.3): pre-seed a WhatsApp
    patient session in PreCheck ahead of any keyword matching, one leg of three.

    brain-api is the entitlement AUTHORITY here — PreCheck is never asked to re-check
    it. Not active/trialing OR `precheck_enabled=false` -> `403 precheck_not_entitled`,
    fail closed, BEFORE any upstream call. Otherwise the outbound leg
    (`services/precheck_handoff.request_handoff`) forwards to PreCheck and maps its
    response 1:1 (see that module's docstring for the full status matrix). No DB write
    on this path; nothing is cached or retried here.

    ### The `external_id` leg (a Portal patient) — no longer a `501`

    TASK-003 §5.1 asked for a Portal patient (`wa_id IS NULL`, so no phone number) to be
    handed off by `external_id`, and stopped at a deliberate `501`: PreCheck's
    `/internal/precheck-handoff` requires `phone_number` (`^\\d{8,15}$`), and the only
    route that takes a `session_ref` was `/internal/brain-message/inbound` — the *inbound*
    route, whose empty-`text` behaviour that task could not read the PreCheck repo to
    verify. TASK-004 read it, and the answer is that `inbound` is safe ONLY at `INIT`:
    past it, an empty text reaches PreCheck's `_turn()` and can save an answer the patient
    never gave. So the fix was not to reuse `inbound` — it was
    `POST {PRECHECK}/internal/brain-message/open`, which never calls the questionnaire
    agent, never writes a patient line, and writes nothing at all on a session that has
    already started. `services/precheck_handoff.request_portal_handoff` is that leg; the
    `501` is gone and unreachable from any valid body.

    Both legs answer with the same two words (`seeded` / `already_active`) and the same
    status matrix, so secretarIA branches once, not per channel.
    """
    ent = await resolve_entitlement(session, payload.tenant_id)
    entitled = ent.status in ACTIVE_STATUSES and ent.products.precheck
    if not entitled:
        logger.info(
            "precheck_handoff_not_entitled",
            tenant_id=str(payload.tenant_id),
            status=ent.status,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "precheck_not_entitled")

    phone_number = payload.phone_number
    if phone_number is None:
        # A PORTAL patient (`external_id`), handed off by handle rather than by phone —
        # see the route docstring for why this is a different upstream route and not the
        # same one with a different field. `booked_service` is not forwarded on this leg
        # (PreCheck's opening route does not declare it, and its model is `extra="forbid"`).
        result = await request_portal_handoff(
            payload.tenant_id,
            str(payload.external_id),
            patient_name=payload.patient_name,
        )
        return PrecheckHandoffOut(status=result["status"])

    # Context fields pass straight through — no gate of their own: "may this clinic
    # receive PreCheck" stays purely ent.status/ent.products.precheck, checked above.
    result = await request_handoff(
        payload.tenant_id,
        phone_number,
        patient_name=payload.patient_name,
        booked_service=payload.booked_service,
    )
    return PrecheckHandoffOut(status=result["status"])


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
        return PendingOtpVerifyOut(status="verified")
    if pending.email is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "pending_email_missing")
    if not _pending_otp_verify_limiter.allow(str(pending.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    verified = await patient_access.verify_pending_identity(session, pending, payload.code)
    if verified is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_or_expired_code")
    return PendingOtpVerifyOut(status="verified")


# --- Onboarding crons (CONTRACT_onboarding_v1.md §5 items 7-8; secretaria pulls/posts) ---


@router.get(
    "/onboarding/tenants",
    response_model=InternalOnboardingListOut,
    summary="Tenants the onboarding crons must still act on (internal)",
    responses=_INTERNAL_RESPONSES,
)
async def list_onboarding_tenants(
    session: AsyncSession = Depends(get_session),
) -> InternalOnboardingListOut:
    """secretaria's `run_onboarding_nudges` cron pulls this hourly to decide who needs a
    retry nudge / config reminder / D+30 manual-review flag / D+60 closing email / (Task 2)
    a past-deadline Meta/WABA test-window email (`test_window_email_due`, computed
    server-side per row — see `services.onboarding_sync.test_window_email_due`).
    Includes every tenant where `onboarding_state != 'ativo'` OR
    `config_status != 'completa'`.
    """
    tenants = await onboarding_sync.list_onboarding_tenants(session)
    if not tenants:
        return InternalOnboardingListOut(items=[])

    settings = get_settings()
    ids = [t.id for t in tenants]
    owners = {
        u.tenant_id: u
        for u in (
            await session.scalars(
                select(User).where(User.tenant_id.in_(ids), User.is_owner.is_(True))
            )
        ).all()
    }
    entitlements = {
        e.tenant_id: e
        for e in (
            await session.scalars(select(Entitlement).where(Entitlement.tenant_id.in_(ids)))
        ).all()
    }

    items = []
    for t in tenants:
        ent = entitlements.get(t.id)
        owner = owners.get(t.id)
        items.append(
            InternalOnboardingTenantOut(
                tenant_id=t.id,
                onboarding_state=t.onboarding_state,
                blocker_reason=t.blocker_reason,
                config_status=t.config_status,
                onboarding_anchor_at=t.onboarding_anchor_at,
                next_retry_at=t.next_retry_at,
                retry_paused=t.retry_paused,
                config_reminder_paused=t.config_reminder_paused,
                config_reminder_anchor_at=t.config_reminder_anchor_at,
                last_config_reminder_at=t.last_config_reminder_at,
                closing_email_sent_at=t.closing_email_sent_at,
                manual_review_flagged_at=t.manual_review_flagged_at,
                owner_email=owner.email if owner else None,
                owner_name=owner.name if owner else None,
                clinic_name=t.clinic_name,
                subscription_active=ent is not None and ent.status in ACTIVE_STATUSES,
                # Task 2: Meta/WABA acceptance test-window reframe.
                test_window_email_due=onboarding_sync.test_window_email_due(t, ent, settings),
                test_window_days=settings.STRIPE_TRIAL_PERIOD_DAYS,
                test_window_restart_url=f"{settings.FRONTEND_BASE_URL}/app/reativar",
            )
        )
    return InternalOnboardingListOut(items=items)


@router.post(
    "/onboarding/tenants/{tenant_id}/events",
    response_model=InternalOnboardingEventOut,
    summary="Record a cron bookkeeping event for a tenant (internal)",
    responses={**_INTERNAL_RESPONSES, 404: {"description": "Unknown tenant."}},
)
async def post_onboarding_event(
    tenant_id: Annotated[UUID, Path(description="Tenant UUID.")],
    payload: InternalOnboardingEventIn,
    session: AsyncSession = Depends(get_session),
) -> InternalOnboardingEventOut:
    """CONTRACT_onboarding_v1.md §5 item 8. `closing_email_sent`/`manual_review_flagged`
    are one-shot (idempotent no-op once already set, `applied:false`);
    `retry_nudge_sent`/`config_reminder_sent` are recurring and always apply."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant_not_found")
    applied = await onboarding_sync.apply_onboarding_event(session, tenant, payload)
    return InternalOnboardingEventOut(applied=applied)
