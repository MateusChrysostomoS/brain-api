"""Doctor onboarding + multi-professional endpoints (CONTRACT_onboarding_v1.md §7).

EVERY route here is gated by `require_doctor` at the router level (same convention as
`api/doctor.py`): the JWT must be valid, carry a `tenant_id`, and have role
`tenant_owner`/`tenant_staff`; a platform `admin` token gets 403. ONE route is further
restricted to the tenant OWNER (`require_tenant_owner`): the onboarding kill-switch pause
— a deliberately owner-only lever, out of the day-to-day "configuracao" surface.
(Corrections round, 2026-07-22: the professional invite/self-bind actions used to be
owner-only too; they are now open to any doctor — owner OR staff — since day-to-day
professional management belongs on that same configuracao surface. Pause stays owner-only
on purpose.) The acting tenant is ALWAYS `principal.tenant_id` from the validated token;
`tenant_id` is never accepted as a query/body param.

State-machine WRITES route through `services/onboarding.py`'s pure functions (never
mutate `tenant.onboarding_state`/`blocker_reason` directly here) and
`services/onboarding_sync.py`'s I/O orchestration (secretaria bridge, config-status
pull, ativo transition). Outbound calls (secretaria, Meta Graph) live in
`services/secretaria_provisioning.py` / `services/meta_graph.py`.
"""

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, require_doctor, require_tenant_owner
from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.core.security import hash_password, hash_refresh_token
from brain_api.models import Entitlement, SignupAttempt, Tenant, User
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
    TestWindowOut,
    TestWindowRestartOut,
)
from brain_api.schemas.signup import IntakeIn
from brain_api.services import (
    billing,
    catalog,
    meta_graph,
    onboarding,
    onboarding_sync,
    secretaria_provisioning,
    signup as signup_service,
)

logger = get_logger(__name__)

# Router-level gate: every /doctor/onboarding* + /doctor/professionals* route requires a
# tenant_owner/tenant_staff token (403 else). The one remaining owner-only route (pause)
# additionally depends on require_tenant_owner (corrections round, 2026-07-22: invites/
# self used to be owner-only too — see the module docstring).
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

    if tenant.onboarding_state == onboarding.STATE_CONECTADO:
        # Task 2 (test-window reframe): the Stripe trial exists to cover the Meta/WABA
        # acceptance test window, not general onboarding friction — a tenant reaching
        # 'conectado' has cleared that bar, so the billing cycle anchors HERE rather than
        # waiting for 'ativo'. Same call `onboarding_sync.refresh_config_status` makes
        # later at its own 'ativo' transition; `harden_charge` is idempotent
        # (`charge_hardened_at` guard), so the two can never double-charge.
        await billing.harden_charge(session, tenant)

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


# --- POST /doctor/onboarding/intake -------------------------------------------------------


@router.post(
    "/onboarding/intake",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Attach the pre-checkout eligibility intake to the signup intent",
    description=(
        "The authenticated mid-wizard counterpart of registration: the /cadastro wizard "
        "collects the WhatsApp-usage / prior-API / Facebook-page answers AFTER the first "
        "card (by which point the visitor is logged in), and attaches them to the pending "
        "signup intent so the Stripe webhook can seed the tenant's initial onboarding "
        "state (services.signup.provision_tenant_from_intent). Best-effort and idempotent: "
        "always 204, even when there is no pending intent to attach to."
    ),
)
async def attach_intake(
    payload: IntakeIn,
    principal: Principal = Depends(require_doctor),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Store the intake on the caller's own tenant's pending signup intent (scoped by the
    token — `tenant_id` is never accepted from the client)."""
    await signup_service.attach_intake(session, principal.tenant_id, payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    summary="Invite a professional",
    responses={
        409: {"description": "Email already registered."},
        502: {"description": "secretaria unavailable."},
    },
)
async def invite_professional(
    payload: ProfessionalInviteIn,
    principal: Principal = Depends(require_doctor),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalInviteOut:
    """Create-or-attach the secretaria professional, then a local `tenant_staff` user
    bound to it, then mint a single-use invite token
    (`POST /auth/exchange-invite-token`). The `professional_invite` email is fail-soft —
    the response ALWAYS carries `invite_link` so the caller can share it manually.

    Open to any doctor (owner OR staff) — corrections round, 2026-07-22; previously
    owner-only (`require_tenant_owner`).
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
    summary="Bind the caller to a professional",
    responses={
        409: {"description": "The caller is already bound to a professional."},
        502: {"description": "secretaria unavailable."},
    },
)
async def bind_self_professional(
    payload: ProfessionalSelfIn,
    principal: Principal = Depends(require_doctor),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalSelfOut:
    """Bind the CALLING user (owner or staff) to a professional — open to any doctor
    since the corrections round, 2026-07-22; previously owner-only (`require_tenant_owner`).
    """
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


# --- GET/POST /doctor/onboarding/test-window (Task 2: Meta/WABA acceptance window) -------


def _test_window_deadline(tenant: Tenant, days: int) -> datetime | None:
    """`test_window_started_at + days`, tz-normalized (SQLite hands back naive datetimes;
    Postgres aware — same idiom as `services/onboarding_sync.py`). `None` when the window
    has not started yet."""
    started_at = tenant.test_window_started_at
    if started_at is None:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return started_at + timedelta(days=days)


@router.get(
    "/onboarding/test-window",
    response_model=TestWindowOut,
    summary="Meta/WABA acceptance test-window status",
)
async def get_test_window(
    principal: Principal = Depends(require_doctor),
    session: AsyncSession = Depends(get_session),
) -> TestWindowOut:
    """The doctor-facing live read behind Task 2's reframing: same plan/day/subscription
    gates as the internal cron's `test_window_email_due` (services/onboarding_sync.py),
    minus the deadline/notified requirements, so the portal can show progress before (and
    after) the window ever expires.
    """
    tenant = await _load_tenant(session, principal.tenant_id)
    ent = await session.get(Entitlement, tenant.id)
    settings = get_settings()
    days_total = settings.STRIPE_TRIAL_PERIOD_DAYS
    plan = catalog.get_plan(ent.plan) if ent is not None else None

    applicable = bool(
        plan is not None
        and plan.secretaria
        and days_total > 0
        and ent is not None
        and (ent.stripe_subscription_id or ent.stripe_customer_id)
    )
    still_open = tenant.onboarding_state not in (onboarding.STATE_CONECTADO, onboarding.STATE_ATIVO)
    deadline_at = _test_window_deadline(tenant, days_total)
    expired = bool(deadline_at is not None and still_open and datetime.now(UTC) >= deadline_at)

    return TestWindowOut(
        applicable=applicable,
        days_total=days_total,
        started_at=tenant.test_window_started_at,
        deadline_at=deadline_at,
        onboarding_state=tenant.onboarding_state,
        connected_at=tenant.connected_at,
        expired=expired,
        notified=tenant.test_window_notified_at is not None,
        subscription_status=ent.status if ent is not None else None,
        can_restart=applicable and still_open,
    )


@router.post(
    "/onboarding/test-window/restart",
    response_model=TestWindowRestartOut,
    summary="Restart the Meta/WABA acceptance test window",
    responses={
        409: {"description": "test_window_not_applicable | already_connected | checkout_required"},
    },
)
async def restart_test_window(
    principal: Principal = Depends(require_doctor),
    session: AsyncSession = Depends(get_session),
) -> TestWindowRestartOut:
    """Re-checkout without a NEW Checkout Session. When the tenant's Stripe subscription is
    still live (not `canceled`/`incomplete_expired` — typically the common case: the
    original trial hasn't been auto-cancelled yet), extend its `trial_end` in place. When
    it is already gone (the usual post-deadline case: `trial_will_end` already scheduled
    and fired a `cancel_at`), create a brand-new subscription against the same customer,
    reusing a saved card as `default_payment_method` when one exists and re-deriving the
    Checkout line items from the tenant's OWN entitlement (plan + active add-ons) via the
    same `validate_selection` the checkout endpoints use. Either branch calls every Stripe
    endpoint BEFORE touching any row, then restarts the window and commits once — a Stripe
    failure never half-commits.
    """
    tenant = await _load_tenant(session, principal.tenant_id)
    settings = get_settings()
    ent = await session.get(Entitlement, tenant.id)
    plan = catalog.get_plan(ent.plan) if ent is not None else None

    if settings.STRIPE_TRIAL_PERIOD_DAYS <= 0 or plan is None or not plan.secretaria:
        raise HTTPException(status.HTTP_409_CONFLICT, "test_window_not_applicable")
    if tenant.onboarding_state in (onboarding.STATE_CONECTADO, onboarding.STATE_ATIVO):
        raise HTTPException(status.HTTP_409_CONFLICT, "already_connected")
    if not ent.stripe_customer_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "checkout_required")

    days = settings.STRIPE_TRIAL_PERIOD_DAYS
    now = datetime.now(UTC)

    payment_methods_payload = await billing._stripe_get(
        f"/v1/payment_methods?customer={ent.stripe_customer_id}&type=card"
    )
    payment_methods = payment_methods_payload.get("data") or []
    payment_method_present = bool(payment_methods)

    live_subscription: dict | None = None
    if ent.stripe_subscription_id:
        live_subscription = await billing._stripe_get(
            f"/v1/subscriptions/{ent.stripe_subscription_id}"
        )

    if live_subscription is not None and live_subscription.get("status") not in (
        "canceled",
        "incomplete_expired",
    ):
        await billing._stripe_post(
            f"/v1/subscriptions/{ent.stripe_subscription_id}",
            {
                "trial_end": str(int((now + timedelta(days=days)).timestamp())),
                "proration_behavior": "none",
                "cancel_at": "",
            },
        )
        ent.cancel_scheduled_at = None
    else:
        addon_ids = [addon_id for addon_id, active in (ent.addons or {}).items() if active]
        selection = billing.validate_selection(ent.plan, addon_ids)

        data: dict[str, str] = {
            "customer": ent.stripe_customer_id,
            "trial_period_days": str(days),
            "proration_behavior": "none",
            "metadata[tenant_id]": str(tenant.id),
        }
        billing._append_subscription_items(data, selection)
        if payment_methods:
            data["default_payment_method"] = payment_methods[0]["id"]

        subscription_payload = await billing._stripe_post("/v1/subscriptions", data)
        new_subscription_id = str(subscription_payload["id"])
        billing._reset_markers_if_subscription_changed(ent, new_subscription_id)
        ent.stripe_subscription_id = new_subscription_id

    tenant.test_window_started_at = now
    tenant.test_window_notified_at = None
    await session.commit()

    logger.info("test_window_restarted", tenant_id=str(tenant.id))
    return TestWindowRestartOut(
        restarted=True,
        deadline_at=now + timedelta(days=days),
        payment_method_present=payment_method_present,
    )
