"""Onboarding state-machine I/O — the secretaria bridge, config-status pull, and the
ativo transition (CONTRACT_onboarding_v1.md §8; scope A/B/D of the ENDPOINTS +
INTEGRATION + BILLING build). Pure state-machine LOGIC stays in `services/onboarding.py`
untouched; everything here is the "wire it to the outside world" layer on top of it:
calling `services/secretaria_provisioning.py`, deciding WHEN to call it (throttled), and
driving the resulting `Tenant` mutations + (once ativo) `services.billing.harden_charge`.
Also home to the read/write helpers backing the internal `/internal/onboarding/*` surface
the secretaria crons pull (scope D) — they are local-DB-only (no secretaria I/O) but
belong to the same "onboarding orchestration beyond the pure state machine" concern.

In-process caching (no Redis in this service, mirrors `core/ratelimit.py`'s rationale):
a per-tenant TTL cache of the last successfully-pulled secretaria config-status payload,
keyed by `Settings.CONFIG_STATUS_PULL_TTL_SECONDS`. Per-PROCESS and resets on restart —
acceptable, since this only throttles how often `GET /doctor/onboarding` (called on every
portal load) re-pulls secretaria; the underlying secretaria row is always the source of
truth and a restart simply forces one extra pull.
"""

from __future__ import annotations

import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import Settings, get_settings
from brain_api.core.logging import get_logger
from brain_api.models import (
    Entitlement,
    PrecheckAccountLink,
    SignupAttempt,
    SignupIntent,
    Tenant,
    User,
)
from brain_api.services import (
    catalog,
    onboarding,
    precheck_provisioning,
    secretaria_provisioning,
)
from brain_api.services.entitlements import resolve_entitlement

if TYPE_CHECKING:
    from brain_api.schemas.internal import InternalOnboardingEventIn

logger = get_logger(__name__)


async def get_owner(session: AsyncSession, tenant_id: UUID) -> User | None:
    """The tenant's owner user row (`is_owner=true`), or `None`. Shared by the
    secretaria-provisioning bridge (needs an email to hand secretaria) and
    `api/onboarding.py` (needs an email for the connection-success notification)."""
    return await session.scalar(
        select(User).where(User.tenant_id == tenant_id, User.is_owner.is_(True))
    )


# --- Provisioning bridge (scope A) ------------------------------------------------------


async def ensure_secretaria_provisioned(session: AsyncSession, tenant: Tenant) -> None:
    """Best-effort `POST /internal/tenants` bridge; idempotent and GUARANTEED NEVER TO
    RAISE.

    No-op once `tenant.secretaria_provisioned_at` is set. Called from two places: right
    after a fresh tenant is provisioned (the Stripe webhook apply path,
    `services.billing.apply_stripe_event`, post-commit) and as a lazy retry on every
    `GET /doctor/onboarding` (so a secretaria outage at signup time self-heals the first
    time the owner opens the portal). On success, stamps `secretaria_provisioned_at` and
    commits; on any failure, logs a warning and leaves the tenant unprovisioned for the
    next retry. A secretaria outage must never break the webhook or the doctor portal.
    """
    if tenant.secretaria_provisioned_at is not None:
        return
    try:
        owner = await get_owner(session, tenant.id)
        ok = await secretaria_provisioning.provision_tenant(
            tenant.id,
            clinic_name=tenant.clinic_name,
            contact_email=owner.email if owner else None,
            timezone="America/Sao_Paulo",
        )
        if not ok:
            return
        tenant.secretaria_provisioned_at = datetime.now(UTC)
        await session.commit()
        logger.info("secretaria_provisioning_bridge_ok", tenant_id=str(tenant.id))
    except Exception:  # noqa: BLE001 - fail-soft by contract: never break the caller.
        logger.warning(
            "secretaria_provisioning_bridge_failed", tenant_id=str(tenant.id), exc_info=True
        )


# --- Provisioning bridge: PreCheck (scope A, sibling of the secretaria one above) -------


#: Template de especialidade usado quando o intent não carrega escolha — intents
#: anteriores à 0015, ou uma compra feita fora da vitrine do PreCheck.
DEFAULT_PRECHECK_TEMPLATE = "clinica-geral"


def _precheck_identity(clinic_name: str, tenant_id: UUID) -> tuple[str, str]:
    """(clinic_slug, trigger_phrase) determinísticos para uma clínica do PreCheck.

    PreCheck exige os dois únicos: o slug é PK natural da clínica e a frase é o que
    faz o dispatcher rotear o paciente (o match é `frase in mensagem`, então frases
    curtas colidem). Derivar do nome e desempatar com um sufixo do tenant_id dá
    unicidade sem precisar de round-trip de consulta — e, sendo determinístico,
    uma retentativa gera exatamente os mesmos valores da tentativa anterior.
    """
    base = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", clinic_name)
                  .encode("ascii", "ignore").decode().lower()).strip("-") or "clinica"
    sufixo = str(tenant_id)[:8]
    slug = f"{base[:60]}-{sufixo}"
    return slug, f"precheck {base[:40]} {sufixo}"


async def ensure_precheck_provisioned(session: AsyncSession, tenant: Tenant) -> None:
    """Best-effort `POST /internal/provision` bridge; idempotente e GARANTIDO NUNCA LEVANTAR.

    Irmão de `ensure_secretaria_provisioned`, com a mesma história de retry: no-op depois
    que `tenant.precheck_provisioned_at` está carimbado, chamado do webhook do Stripe
    (`services.billing.apply_stripe_event`, pós-commit) e como retry preguiçoso em
    `GET /doctor/onboarding` — então uma indisponibilidade do PreCheck na hora da compra
    se cura sozinha no primeiro load do portal.

    Faz DUAS escritas, e as duas importam:

    1. A clínica no PreCheck (via `POST /internal/provision`, idempotente por
       `brain_tenant_id` do lado de lá).
    2. A linha em `precheck_account_links` — o mapa `brain_user_id -> precheck_user_id`
       que `POST /sso/precheck/token` EXIGE. Sem ela o hand-off responde
       `409 precheck_account_not_linked` e o médico pagante não entra em nada. Era
       populada à mão por `scripts/link_precheck_account.py`.

    Sai fora, sem carimbar, quando o tenant não tem direito ao PreCheck — um comprador
    só de outro produto não deve ganhar clínica.
    """
    if tenant.precheck_provisioned_at is not None:
        return
    try:
        ent = await resolve_entitlement(session, tenant.id)
        if not ent.products.precheck:
            return

        owner = await get_owner(session, tenant.id)
        if owner is None:
            logger.warning("precheck_provisioning_no_owner", tenant_id=str(tenant.id))
            return

        intent = await session.scalar(
            select(SignupIntent)
            .where(SignupIntent.tenant_id == tenant.id)
            .order_by(SignupIntent.created_at.desc())
        )
        template = (
            intent.precheck_template_slug
            if intent is not None and intent.precheck_template_slug
            else DEFAULT_PRECHECK_TEMPLATE
        )

        slug, trigger = _precheck_identity(tenant.clinic_name, tenant.id)
        # O médico NUNCA usa esta senha: entra por SSO (POST /sso/precheck/token), e o
        # brain é a autoridade de identidade. O PreCheck exige o campo, então mandamos
        # um segredo aleatório que ninguém precisa conhecer — mandar a senha real seria
        # impossível (temos só o hash) e copiá-la para outro serviço seria pior.
        body = await precheck_provisioning.provision_clinic(
            tenant.id,
            template_slug=template,
            clinic_name=tenant.clinic_name,
            clinic_slug=slug,
            trigger_phrase=trigger,
            doctor_name=owner.name,
            doctor_email=owner.email,
            doctor_password=secrets.token_urlsafe(32),
            # o telefone vive no intent (o Tenant não tem essa coluna)
            doctor_phone=intent.whatsapp_phone if intent is not None else None,
        )
        if body is None:
            return

        precheck_user_id = body.get("doctor_user_id")
        if not isinstance(precheck_user_id, int):
            logger.warning(
                "precheck_provisioning_no_doctor_user_id", tenant_id=str(tenant.id)
            )
            return

        # Upsert do link: a reverse-unique de precheck_user_id (0002) transforma uma
        # segunda tentativa numa violação de constraint, não num duplicado.
        link = await session.scalar(
            select(PrecheckAccountLink).where(
                PrecheckAccountLink.brain_user_id == owner.id
            )
        )
        if link is None:
            session.add(
                PrecheckAccountLink(
                    brain_user_id=owner.id,
                    precheck_user_id=precheck_user_id,
                    tenant_id=tenant.id,
                )
            )
        else:
            link.precheck_user_id = precheck_user_id
            link.tenant_id = tenant.id

        tenant.precheck_provisioned_at = datetime.now(UTC)
        await session.commit()
        logger.info(
            "precheck_provisioning_bridge_ok",
            tenant_id=str(tenant.id),
            created=bool(body.get("created")),
            template=template,
            precheck_user_id=precheck_user_id,
        )
    except Exception:  # noqa: BLE001 - fail-soft por contrato: nunca quebrar o chamador.
        await session.rollback()
        logger.warning(
            "precheck_provisioning_bridge_failed", tenant_id=str(tenant.id), exc_info=True
        )


# --- Config-status pull + ativo transition (scope B) ------------------------------------


@dataclass
class _ConfigStatusCacheEntry:
    pulled_at: float  # time.monotonic()
    payload: dict[str, Any]


#: tenant_id -> last successfully-pulled config-status payload + when it was pulled.
#: Module-level (per-process) cache — see the module docstring.
_config_status_cache: dict[UUID, _ConfigStatusCacheEntry] = {}


def get_cached_config_status(tenant_id: UUID) -> dict[str, Any] | None:
    """The last successfully-pulled config-status payload for a tenant (this process),
    or `None` if it has never been pulled. Freshness is governed by
    `refresh_config_status`'s own TTL throttle — callers that need it current call that
    FIRST (both `GET /doctor/onboarding` and `GET /doctor/professionals` do)."""
    entry = _config_status_cache.get(tenant_id)
    return entry.payload if entry is not None else None


def mode_resolved_hint(tenant_id: UUID) -> bool:
    """Best-effort `mode_resolved` for the `GET /doctor/onboarding` response. secretaria
    owns this flag (its own `mode_resolved_at` column); brain-api never persists it
    locally, only mirrors the last pulled value."""
    payload = get_cached_config_status(tenant_id)
    return bool(payload.get("mode_resolved")) if payload else False


def _mode_resolved_or_fallback_due(
    tenant: Tenant, payload: dict[str, Any], settings: Settings
) -> bool:
    if payload.get("mode_resolved"):
        return True
    if tenant.connected_at is None:
        return False
    connected_at = tenant.connected_at
    if connected_at.tzinfo is None:  # SQLite returns naive; Postgres aware.
        connected_at = connected_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - connected_at >= timedelta(hours=settings.MODE_RESOLVE_FALLBACK_HOURS)


async def refresh_config_status(session: AsyncSession, tenant: Tenant) -> None:
    """Throttled pull of secretaria's config-status + the ativo-transition check.

    Throttled per-tenant by `Settings.CONFIG_STATUS_PULL_TTL_SECONDS` (module-level
    cache, see the module docstring) — a no-op when the last pull is still fresh.
    Otherwise: pulls `GET /internal/tenants/{id}/config-status`, applies
    `services.onboarding.apply_config_status`, and — when state=='conectado' AND
    config_status=='completa' AND (secretaria says `mode_resolved` OR `connected_at` is
    older than `Settings.MODE_RESOLVE_FALLBACK_HOURS`) — calls secretaria `/activate`;
    only a genuine `{"activated": true}` flips the tenant to 'ativo' and triggers
    `services.billing.harden_charge`. Fully fail-soft: any failure (including a
    secretaria outage) just skips this refresh, leaving the tenant's existing state
    untouched for the next call. Never raises.
    """
    settings = get_settings()
    now = time.monotonic()
    entry = _config_status_cache.get(tenant.id)
    if entry is not None and (now - entry.pulled_at) < settings.CONFIG_STATUS_PULL_TTL_SECONDS:
        return

    try:
        payload = await secretaria_provisioning.get_config_status(tenant.id)
        if payload is None:
            return
        _config_status_cache[tenant.id] = _ConfigStatusCacheEntry(pulled_at=now, payload=payload)

        onboarding.apply_config_status(
            tenant,
            connected=bool(payload.get("connected")),
            config_complete=bool(payload.get("config_complete")),
        )

        activated = False
        if (
            tenant.onboarding_state == onboarding.STATE_CONECTADO
            and tenant.config_status == onboarding.CONFIG_STATUS_COMPLETA
            and _mode_resolved_or_fallback_due(tenant, payload, settings)
        ):
            activate_payload = await secretaria_provisioning.activate_tenant(tenant.id)
            if activate_payload is not None and activate_payload.get("activated"):
                tenant.onboarding_state = onboarding.STATE_ATIVO
                tenant.activated_at = datetime.now(UTC)
                activated = True

        await session.commit()

        if activated:
            # Local import: services.billing does not import onboarding_sync, so there is
            # no cycle today — kept local anyway for symmetry with billing.py's own local
            # import of this module (avoids a load-order footgun if that ever changes).
            from brain_api.services import billing as billing_service

            await billing_service.harden_charge(session, tenant)
    except Exception:  # noqa: BLE001 - fail-soft by contract: never break the caller.
        logger.warning("refresh_config_status_failed", tenant_id=str(tenant.id), exc_info=True)
        await session.rollback()


# --- Last attempt (GET /doctor/onboarding) -----------------------------------------------


async def get_last_attempt(session: AsyncSession, tenant_id: UUID) -> SignupAttempt | None:
    """The most recently recorded `SignupAttempt` for a tenant, or `None`."""
    return await session.scalar(
        select(SignupAttempt)
        .where(SignupAttempt.tenant_id == tenant_id)
        .order_by(SignupAttempt.created_at.desc())
        .limit(1)
    )


# --- Internal listing + event bookkeeping (scope D; secretaria's crons pull/post this) ---


async def list_onboarding_tenants(session: AsyncSession) -> list[Tenant]:
    """Tenants the crons must still act on (CONTRACT_onboarding_v1.md §5 item 7):
    `onboarding_state != 'ativo'` OR `config_status != 'completa'`. Returns bare `Tenant`
    rows; `api/internal.py` batch-loads the owner + entitlement side data (mirrors
    `services/admin.py::list_tenants`'s batched-query style, avoiding N+1 per tenant)."""
    rows = await session.scalars(
        select(Tenant).where(
            or_(
                Tenant.onboarding_state != onboarding.STATE_ATIVO,
                Tenant.config_status != onboarding.CONFIG_STATUS_COMPLETA,
            )
        )
    )
    return list(rows.all())


def test_window_email_due(
    tenant: Tenant, ent: Entitlement | None, settings: Settings, *, now: datetime | None = None
) -> bool:
    """Task 2 (Meta/WABA acceptance test-window reframe): whether
    `GET /internal/onboarding/tenants` should flag `test_window_email_due` for this
    tenant — the paid Stripe trial (`STRIPE_TRIAL_PERIOD_DAYS`) ran out before the tenant
    ever reached 'conectado'/'ativo', and nobody has been notified yet.

    Deliberately does NOT require `ent.status` to be active/trialing: by the deadline the
    `customer.subscription.trial_will_end` handler (`services/billing.py::apply_stripe_event`)
    has typically already scheduled — and Stripe already fired — the auto-cancellation, so
    an auto-cancelled, never-connected, paid secretarIA subscriber is EXACTLY the
    population this email targets, not an edge case to exclude.
    """
    if settings.STRIPE_TRIAL_PERIOD_DAYS <= 0:
        return False
    if tenant.test_window_started_at is None or tenant.test_window_notified_at is not None:
        return False
    if tenant.onboarding_state in (onboarding.STATE_CONECTADO, onboarding.STATE_ATIVO):
        return False
    if ent is None or ent.stripe_subscription_id is None:
        return False
    plan = catalog.get_plan(ent.plan)
    if plan is None or not plan.secretaria:
        return False

    started_at = tenant.test_window_started_at
    if started_at.tzinfo is None:  # SQLite returns naive; Postgres aware.
        started_at = started_at.replace(tzinfo=UTC)
    deadline = started_at + timedelta(days=settings.STRIPE_TRIAL_PERIOD_DAYS)
    return (now or datetime.now(UTC)) >= deadline


#: Event names that fire exactly ONCE per tenant (CONTRACT_onboarding_v1.md §11: D+30
#: manual-review flag, D+60 closing email; Task 2: past-deadline test-window email) —
#: applying one that is already set is a no-op. `retry_nudge_sent` / `config_reminder_sent`
#: are RECURRING (the crons re-fire them on every cadence tick) and are deliberately NOT in
#: this set: they always apply.
_ONE_SHOT_EVENTS = ("closing_email_sent", "manual_review_flagged", "test_window_email_sent")

#: The one-shot events above assume their Tenant marker column is named `f"{event}_at"` —
#: true for the first two, but "test_window_email_sent"'s marker is `test_window_notified_at`
#: (Task 2 fixes that name to match `GET /doctor/onboarding/test-window`'s `notified` field
#: and `test_window_email_due`'s own gate above). Maps the exception; every other one-shot
#: event still resolves via the `f"{event}_at"` convention.
_ONE_SHOT_MARKER_COLUMN = {"test_window_email_sent": "test_window_notified_at"}


async def apply_onboarding_event(
    session: AsyncSession, tenant: Tenant, payload: InternalOnboardingEventIn
) -> bool:
    """Apply one cron-reported event (`POST /internal/onboarding/tenants/{id}/events`,
    CONTRACT_onboarding_v1.md §5 item 8). Returns whether it was applied — `False` for an
    already-set one-shot marker (idempotent against a redelivered cron tick), never an
    HTTP error either way.
    """
    event = payload.event
    if event in _ONE_SHOT_EVENTS:
        marker_column = _ONE_SHOT_MARKER_COLUMN.get(event, f"{event}_at")
        if getattr(tenant, marker_column) is not None:
            return False

    if event == "retry_nudge_sent":
        tenant.next_retry_at = payload.next_retry_at
    elif event == "config_reminder_sent":
        tenant.last_config_reminder_at = payload.at
    elif event == "closing_email_sent":
        tenant.closing_email_sent_at = payload.at
    elif event == "manual_review_flagged":
        tenant.manual_review_flagged_at = payload.at
    elif event == "test_window_email_sent":
        tenant.test_window_notified_at = payload.at
    else:  # pragma: no cover - schemas.internal.InternalOnboardingEventIn already
        # constrains `event` to a Literal of the branches above.
        return False

    await session.commit()
    return True
