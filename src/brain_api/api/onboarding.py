"""Doctor onboarding + multi-professional endpoints (CONTRACT_onboarding_v1.md §7).

EVERY route here is gated by `require_doctor` at the router level (same convention as
`api/doctor.py`): the JWT must be valid, carry a `tenant_id`, and have role
`tenant_owner`/`tenant_staff`; a platform `admin` token gets 403. A handful of routes are
further restricted to the tenant OWNER (`require_tenant_owner`) — the kill-switch pause
and the professional invite/self-bind actions. The acting tenant is ALWAYS
`principal.tenant_id` from the validated token; `tenant_id` is never accepted as a
query/body param.

State-machine WRITES route through `services/onboarding.py`'s pure functions (never
mutate `tenant.onboarding_state`/`blocker_reason` directly here) and
`services/onboarding_sync.py`'s I/O orchestration (secretaria bridge, config-status
pull, ativo transition). Outbound calls (secretaria, Meta Graph) live in
`services/secretaria_provisioning.py` / `services/meta_graph.py`.
"""

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, require_doctor, require_tenant_owner
from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.core.security import hash_password, hash_refresh_token
from brain_api.models import SignupAttempt, Tenant, User
from brain_api.models.user import ROLE_TENANT_STAFF
from brain_api.schemas.onboarding import (
    AttemptIn,
    AttemptOut,
    EmbeddedSignupOut,
    LastAttemptOut,
    OnboardingActionOut,
    OnboardingStateOut,
    PauseIn,
    ProfessionalInviteIn,
    ProfessionalInviteOut,
    ProfessionalOut,
    ProfessionalSelfIn,
    ProfessionalSelfOut,
    ProfessionalsOut,
)
from brain_api.services import meta_graph, onboarding, onboarding_sync, secretaria_provisioning

logger = get_logger(__name__)

# Router-level gate: every /doctor/onboarding* + /doctor/professionals* route requires a
# tenant_owner/tenant_staff token (403 else). Owner-only routes add require_tenant_owner.
router = APIRouter(prefix="/doctor", dependencies=[Depends(require_doctor)])


async def _load_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant:
    """Defensive: a valid doctor token always resolves to a real tenant row; guard
    anyway rather than let a dangling FK 500 the request."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:  # pragma: no cover - see docstring.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant_not_found")
    return tenant


def _last_attempt_out(row: SignupAttempt | None) -> LastAttemptOut | None:
    if row is None:
        return None
    return LastAttemptOut(
        attempt_id=row.id,
        result=row.result,
        blocker_reason=row.blocker_reason,
        error_code=row.error_code,
        created_at=row.created_at,
    )


def _embedded_signup_out() -> EmbeddedSignupOut:
    settings = get_settings()
    return EmbeddedSignupOut(
        configured=bool(settings.META_APP_ID and settings.META_ES_CONFIG_ID),
        app_id=settings.META_APP_ID or None,
        config_id=settings.META_ES_CONFIG_ID or None,
    )


def _action_out(tenant: Tenant) -> OnboardingActionOut:
    return OnboardingActionOut(
        onboarding_state=tenant.onboarding_state, blocker_reason=tenant.blocker_reason
    )


# --- GET /doctor/onboarding ---------------------------------------------------------------


@router.get("/onboarding", response_model=OnboardingStateOut, summary="Onboarding state")
async def get_onboarding(
    principal: Principal = Depends(require_doctor),
    session: AsyncSession = Depends(get_session),
) -> OnboardingStateOut:
    """The eligibility-screen state (CONTRACT_onboarding_v1.md §7). Side effects, both
    throttled/fail-soft: lazily retries the secretaria provisioning bridge, then pulls
    secretaria's config-status and runs the ativo-transition check."""
    tenant = await _load_tenant(session, principal.tenant_id)
    await onboarding_sync.ensure_secretaria_provisioned(session, tenant)
    await onboarding_sync.refresh_config_status(session, tenant)

    last_attempt = await onboarding_sync.get_last_attempt(session, tenant.id)
    return OnboardingStateOut(
        onboarding_state=tenant.onboarding_state,
        blocker_reason=tenant.blocker_reason,
        config_status=tenant.config_status,
        connected=tenant.connected_at is not None,
        mode_resolved=onboarding_sync.mode_resolved_hint(tenant.id),
        secretaria_provisioned=tenant.secretaria_provisioned_at is not None,
        next_retry_at=tenant.next_retry_at,
        retry_paused=tenant.retry_paused,
        config_reminder_paused=tenant.config_reminder_paused,
        last_attempt=_last_attempt_out(last_attempt),
        embedded_signup=_embedded_signup_out(),
    )


# --- POST /doctor/onboarding/attempts -----------------------------------------------------


async def _record_fail(
    session: AsyncSession, tenant: Tenant, attempt_id: UUID, error_code: str | None
) -> tuple[SignupAttempt, bool]:
    row, is_new = await onboarding.record_attempt(
        session,
        tenant,
        attempt_id=attempt_id,
        source=onboarding.ATTEMPT_SOURCE_USER,
        result=onboarding.ATTEMPT_RESULT_FAIL,
        blocker_reason=tenant.blocker_reason,
        error_code=error_code,
        day_offset=None,
    )
    await session.commit()
    return row, is_new


@router.post(
    "/onboarding/attempts",
    response_model=AttemptOut,
    summary="Record a WhatsApp-connection attempt",
    responses={422: {"description": "phone_number_id missing for a pass result."}},
)
async def post_attempt(
    payload: AttemptIn,
    principal: Principal = Depends(require_doctor),
    session: AsyncSession = Depends(get_session),
) -> AttemptOut:
    """CONTRACT_onboarding_v1.md §7. Idempotent on `attempt_id`: a replay short-circuits
    BEFORE any Meta/secretaria I/O and returns the tenant's CURRENT state unchanged — no
    re-exchange, no re-connection call, no double transition.

    On `result=='pass'`: optionally exchanges `code` via Meta Graph (skipped when
    absent; a failure here becomes a 'fail' attempt with
    `error_code='token_exchange_failed'`), then calls secretaria's whatsapp-connection.
    ONLY a successful connection reaches `record_attempt(result='pass')` — a 409
    (`phone_number_conflict`) or any other failure instead records a 'fail' attempt with
    the matching `error_code`, never a 5xx to the caller. A genuine pass then fires a
    fire-and-forget `connection_success` email and a `refresh_config_status` pull.
    """
    tenant = await _load_tenant(session, principal.tenant_id)

    existing = await session.get(SignupAttempt, payload.attempt_id)
    if existing is not None:
        return AttemptOut(
            attempt_id=existing.id,
            replayed=True,
            onboarding_state=tenant.onboarding_state,
            blocker_reason=tenant.blocker_reason,
        )

    if payload.result == onboarding.ATTEMPT_RESULT_FAIL:
        row, is_new = await _record_fail(session, tenant, payload.attempt_id, payload.error_code)
        return AttemptOut(
            attempt_id=row.id,
            replayed=not is_new,
            onboarding_state=tenant.onboarding_state,
            blocker_reason=tenant.blocker_reason,
        )

    # result == "pass" (the schema guarantees phone_number_id is set on this branch).
    access_token: str | None = None
    if payload.code:
        access_token = await meta_graph.exchange_code_for_token(payload.code)
        if access_token is None:
            row, is_new = await _record_fail(
                session, tenant, payload.attempt_id, "token_exchange_failed"
            )
            return AttemptOut(
                attempt_id=row.id,
                replayed=not is_new,
                onboarding_state=tenant.onboarding_state,
                blocker_reason=tenant.blocker_reason,
            )

    outcome = await secretaria_provisioning.connect_whatsapp(
        tenant.id,
        phone_number_id=payload.phone_number_id,
        waba_id=payload.waba_id,
        access_token=access_token,
    )
    if outcome == secretaria_provisioning.CONNECTION_CONFLICT:
        row, is_new = await _record_fail(
            session, tenant, payload.attempt_id, "phone_number_conflict"
        )
        return AttemptOut(
            attempt_id=row.id,
            replayed=not is_new,
            onboarding_state=tenant.onboarding_state,
            blocker_reason=tenant.blocker_reason,
        )
    if outcome != secretaria_provisioning.CONNECTION_OK:
        row, is_new = await _record_fail(
            session, tenant, payload.attempt_id, "secretaria_connection_failed"
        )
        return AttemptOut(
            attempt_id=row.id,
            replayed=not is_new,
            onboarding_state=tenant.onboarding_state,
            blocker_reason=tenant.blocker_reason,
        )

    # Only a successful secretaria connection ever reaches record_attempt(result="pass").
    row, is_new = await onboarding.record_attempt(
        session,
        tenant,
        attempt_id=payload.attempt_id,
        source=onboarding.ATTEMPT_SOURCE_USER,
        result=onboarding.ATTEMPT_RESULT_PASS,
        blocker_reason=tenant.blocker_reason,
        error_code=None,
        day_offset=None,
    )
    await session.commit()

    owner = await onboarding_sync.get_owner(session, tenant.id)
    if owner is not None:
        # Fire-and-forget: send_notification_email never raises and its result is not
        # awaited-into-a-decision here (CONTRACT_onboarding_v1.md scope C).
        await secretaria_provisioning.send_notification_email(
            owner.email, "connection_success", {"clinic_name": tenant.clinic_name}
        )
    await onboarding_sync.refresh_config_status(session, tenant)

    return AttemptOut(
        attempt_id=row.id,
        replayed=not is_new,
        onboarding_state=tenant.onboarding_state,
        blocker_reason=tenant.blocker_reason,
    )


# --- POST /doctor/onboarding/resolve-blocker + pause ---------------------------------------


@router.post(
    "/onboarding/resolve-blocker",
    response_model=OnboardingActionOut,
    summary="Clear a blocker and re-enter aquecimento",
)
async def resolve_blocker(
    principal: Principal = Depends(require_doctor),
    session: AsyncSession = Depends(get_session),
) -> OnboardingActionOut:
    tenant = await _load_tenant(session, principal.tenant_id)
    if onboarding.resolve_blocker(tenant):
        await session.commit()
    return _action_out(tenant)


@router.post(
    "/onboarding/pause",
    response_model=OnboardingActionOut,
    summary="Pause retry / config-reminder nudges (owner only)",
)
async def pause_onboarding(
    payload: PauseIn,
    principal: Principal = Depends(require_tenant_owner),
    session: AsyncSession = Depends(get_session),
) -> OnboardingActionOut:
    tenant = await _load_tenant(session, principal.tenant_id)
    if payload.retries is not None:
        tenant.retry_paused = payload.retries
    if payload.config_reminders is not None:
        tenant.config_reminder_paused = payload.config_reminders
    await session.commit()
    return _action_out(tenant)


# --- GET /doctor/professionals ---------------------------------------------------------------


@router.get(
    "/professionals",
    response_model=ProfessionalsOut,
    summary="Professionals + local invite/link state",
)
async def list_professionals(
    principal: Principal = Depends(require_doctor),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalsOut:
    """Proxies secretaria's config-status `professionals` array, joined with the LOCAL
    brain-api user linkage (email + whether an invite is still pending)."""
    tenant = await _load_tenant(session, principal.tenant_id)
    await onboarding_sync.refresh_config_status(session, tenant)
    payload = onboarding_sync.get_cached_config_status(tenant.id) or {}
    remote_professionals = payload.get("professionals") or []

    linked_users = (
        await session.scalars(
            select(User).where(User.tenant_id == tenant.id, User.professional_id.is_not(None))
        )
    ).all()
    by_professional_id = {str(u.professional_id): u for u in linked_users}

    items: list[ProfessionalOut] = []
    for prof in remote_professionals:
        linked = by_professional_id.get(str(prof.get("id")))
        items.append(
            ProfessionalOut(
                id=str(prof.get("id")),
                name=prof.get("name", ""),
                is_active=bool(prof.get("is_active", True)),
                has_calendar=bool(prof.get("has_calendar", False)),
                has_hours=bool(prof.get("has_hours", False)),
                has_services=bool(prof.get("has_services", False)),
                complete=bool(prof.get("complete", False)),
                linked_user_email=linked.email if linked else None,
                invite_pending=bool(linked and linked.invite_token_hash is not None),
            )
        )
    return ProfessionalsOut(items=items)


@router.post(
    "/professionals/invites",
    response_model=ProfessionalInviteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Invite a professional (owner only)",
    responses={
        409: {"description": "Email already registered."},
        502: {"description": "secretaria unavailable."},
    },
)
async def invite_professional(
    payload: ProfessionalInviteIn,
    principal: Principal = Depends(require_tenant_owner),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalInviteOut:
    """Create-or-attach the secretaria professional, then a local `tenant_staff` user
    bound to it, then mint a single-use invite token
    (`POST /auth/exchange-invite-token`). The `professional_invite` email is fail-soft —
    the response ALWAYS carries `invite_link` so the owner can share it manually.
    """
    tenant = await _load_tenant(session, principal.tenant_id)
    email = payload.email.lower()

    if await session.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email_already_registered")

    professional = await secretaria_provisioning.create_professional(
        tenant.id, name=payload.name, specialty=payload.specialty, about=None
    )
    if professional is None or not professional.get("professional_id"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "secretaria_unavailable")
    professional_id = UUID(str(professional["professional_id"]))

    raw_token = secrets.token_urlsafe(32)
    user = User(
        tenant_id=tenant.id,
        email=email,
        name=payload.name,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        role=ROLE_TENANT_STAFF,
        professional_id=professional_id,
        invite_token_hash=hash_refresh_token(raw_token),
        invite_token_expires_at=datetime.now(UTC)
        + timedelta(hours=get_settings().INVITE_TOKEN_EXPIRE_HOURS),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    invite_link = f"{get_settings().FRONTEND_BASE_URL}/convite?token={raw_token}"
    await secretaria_provisioning.send_notification_email(
        email,
        "professional_invite",
        # secretaria's professional_invite template renders `{link}` (services/email.py).
        {"name": payload.name, "clinic_name": tenant.clinic_name, "link": invite_link},
    )

    logger.info(
        "professional_invited", tenant_id=str(tenant.id), professional_id=str(professional_id)
    )
    return ProfessionalInviteOut(
        professional_id=professional_id, user_id=user.id, invite_link=invite_link
    )


@router.post(
    "/professionals/self",
    response_model=ProfessionalSelfOut,
    summary="Bind the owner to a professional (owner only)",
    responses={
        409: {"description": "The owner is already bound to a professional."},
        502: {"description": "secretaria unavailable."},
    },
)
async def bind_self_professional(
    payload: ProfessionalSelfIn,
    principal: Principal = Depends(require_tenant_owner),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalSelfOut:
    tenant = await _load_tenant(session, principal.tenant_id)
    user = await session.get(User, UUID(principal.user_id))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    if user.professional_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "already_bound")

    name = payload.name or user.name or tenant.clinic_name
    professional = await secretaria_provisioning.create_professional(
        tenant.id, name=name, specialty=payload.specialty, about=None
    )
    if professional is None or not professional.get("professional_id"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "secretaria_unavailable")

    user.professional_id = UUID(str(professional["professional_id"]))
    await session.commit()
    logger.info("professional_self_bound", tenant_id=str(tenant.id), user_id=str(user.id))
    return ProfessionalSelfOut(
        professional_id=user.professional_id, created=bool(professional.get("created"))
    )
