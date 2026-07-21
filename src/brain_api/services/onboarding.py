"""Onboarding state machine — enum constants + PURE transition logic.

Single source of truth for the onboarding/multi-professional state machine
(CONTRACT_onboarding_v1.md §1, §8): every `tenants.onboarding_state` /
`blocker_reason` / `config_status` value, and every `signup_attempts.result` /
`source`, is declared HERE as a named constant — models and callers reference these
constants, never a raw string.

Scope discipline: this module is PURE LOGIC only. No HTTP calls, no secretaria client,
no Stripe — a later build (B2) wires the actual I/O (the secretaria bridge, the Meta
Graph token exchange, the `/doctor/onboarding*` endpoints) on top of these functions.
Every function here either returns a value with no side effect, or mutates the ORM
objects it's handed (`Tenant`/`SignupAttempt`) in memory — the CALLER commits.

The `SignupAttempt` model is imported lazily (inside `record_attempt`, not at module
scope) so this module never touches `brain_api.models` at import time: `models/tenant.py`
imports the constants declared here, so a module-level import of `brain_api.models` back
out of this file would be a circular import. See `models/tenant.py`'s docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from brain_api.core.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from brain_api.models.signup_attempt import SignupAttempt
    from brain_api.models.tenant import Tenant

logger = get_logger(__name__)

# --- onboarding_state (CONTRACT_onboarding_v1.md §1) -----------------------------------

STATE_PENDING = "pending"
STATE_AQUECIMENTO = "aquecimento"
STATE_AGUARDANDO_ELEGIBILIDADE = "aguardando_elegibilidade"
STATE_AGUARDANDO_ACAO_MANUAL = "aguardando_acao_manual"
STATE_CONECTADO = "conectado"
STATE_ATIVO = "ativo"

ONBOARDING_STATES: tuple[str, ...] = (
    STATE_PENDING,
    STATE_AQUECIMENTO,
    STATE_AGUARDANDO_ELEGIBILIDADE,
    STATE_AGUARDANDO_ACAO_MANUAL,
    STATE_CONECTADO,
    STATE_ATIVO,
)

# --- blocker_reason (nullable) ----------------------------------------------------------

BLOCKER_ATIVIDADE_INSUFICIENTE = "atividade_insuficiente"
BLOCKER_NUMERO_EM_OUTRO_BSP = "numero_em_outro_bsp"
BLOCKER_SEM_ACESSO_ADMIN_WABA = "sem_acesso_admin_waba"
BLOCKER_SEM_PAGINA_FACEBOOK = "sem_pagina_facebook"
BLOCKER_OUTRO = "outro"

BLOCKER_REASONS: tuple[str, ...] = (
    BLOCKER_ATIVIDADE_INSUFICIENTE,
    BLOCKER_NUMERO_EM_OUTRO_BSP,
    BLOCKER_SEM_ACESSO_ADMIN_WABA,
    BLOCKER_SEM_PAGINA_FACEBOOK,
    BLOCKER_OUTRO,
)

#: blocker_reasons that put a tenant in aguardando_acao_manual (vs. aguardando_elegibilidade).
_MANUAL_ACTION_BLOCKERS: tuple[str, ...] = (
    BLOCKER_NUMERO_EM_OUTRO_BSP,
    BLOCKER_SEM_ACESSO_ADMIN_WABA,
    BLOCKER_SEM_PAGINA_FACEBOOK,
)

# --- config_status -----------------------------------------------------------------------

CONFIG_STATUS_INCOMPLETA = "incompleta"
CONFIG_STATUS_CONFIGURADO_AGUARDANDO_NUMERO = "configurado_aguardando_numero"
CONFIG_STATUS_COMPLETA = "completa"

CONFIG_STATUSES: tuple[str, ...] = (
    CONFIG_STATUS_INCOMPLETA,
    CONFIG_STATUS_CONFIGURADO_AGUARDANDO_NUMERO,
    CONFIG_STATUS_COMPLETA,
)

# --- signup_attempts.result / .source -----------------------------------------------------

ATTEMPT_RESULT_PASS = "pass"
ATTEMPT_RESULT_FAIL = "fail"
ATTEMPT_RESULTS: tuple[str, ...] = (ATTEMPT_RESULT_PASS, ATTEMPT_RESULT_FAIL)

ATTEMPT_SOURCE_USER = "user"
ATTEMPT_SOURCE_RETRY_NUDGE = "retry_nudge"
ATTEMPT_SOURCE_INTAKE = "intake"
ATTEMPT_SOURCES: tuple[str, ...] = (
    ATTEMPT_SOURCE_USER,
    ATTEMPT_SOURCE_RETRY_NUDGE,
    ATTEMPT_SOURCE_INTAKE,
)

# --- initial-state derivation (CONTRACT_onboarding_v1.md §8) -----------------------------


def derive_initial_state(intake: dict | None) -> tuple[str, str | None]:
    """The (onboarding_state, blocker_reason) a fresh signup intent's intake implies.

    Exact priority order (first match wins):
    1. `prior_api == "yes"`          -> aguardando_acao_manual / numero_em_outro_bsp
    2. `fb_page == "yes_unknown_admin"` -> aguardando_acao_manual / sem_acesso_admin_waba
    3. `fb_page == "no"`             -> aquecimento / sem_pagina_facebook
    4. otherwise (including `intake is None`) -> aquecimento / None
    """
    if intake is None:
        return STATE_AQUECIMENTO, None
    if intake.get("prior_api") == "yes":
        return STATE_AGUARDANDO_ACAO_MANUAL, BLOCKER_NUMERO_EM_OUTRO_BSP
    if intake.get("fb_page") == "yes_unknown_admin":
        return STATE_AGUARDANDO_ACAO_MANUAL, BLOCKER_SEM_ACESSO_ADMIN_WABA
    if intake.get("fb_page") == "no":
        return STATE_AQUECIMENTO, BLOCKER_SEM_PAGINA_FACEBOOK
    return STATE_AQUECIMENTO, None


def initial_next_retry_at(intake: dict | None, now: datetime) -> datetime:
    """First-attempt retry calibration from the intake's declared WhatsApp usage.

    Already using WhatsApp Business heavily (`whatsapp_usage == "business_7d_plus"`)
    -> retry immediately (`now`); anything else (including no intake) -> `now + 3 days`.
    """
    whatsapp_usage = (intake or {}).get("whatsapp_usage")
    if whatsapp_usage == "business_7d_plus":
        return now
    return now + timedelta(days=3)


def map_error_to_blocker(error_code: str | None, declared: str | None) -> str:
    """Map a raw connection error to a `blocker_reason`, always returning a valid one.

    Case-insensitive substring match on `error_code`:
    - contains "previously" / "another" / "in use"  -> numero_em_outro_bsp
    - contains "page" / "permission" / "admin"       -> sem_acesso_admin_waba
    - otherwise -> `declared` when it is a known blocker_reason, else "outro".
    """
    if error_code:
        low = error_code.lower()
        if any(hint in low for hint in ("previously", "another", "in use")):
            return BLOCKER_NUMERO_EM_OUTRO_BSP
        if any(hint in low for hint in ("page", "permission", "admin")):
            return BLOCKER_SEM_ACESSO_ADMIN_WABA
    if declared in BLOCKER_REASONS:
        return declared
    return BLOCKER_OUTRO


# --- attempt recording (idempotent; the sole state-machine writer for attempts) ---------


async def record_attempt(
    session: AsyncSession,
    tenant: Tenant,
    *,
    attempt_id: UUID,
    source: str,
    result: str,
    blocker_reason: str | None,
    error_code: str | None,
    day_offset: int | None,
) -> tuple[SignupAttempt, bool]:
    """Record one connection attempt and apply its state transition, idempotently.

    `attempt_id` IS the idempotency key (mirrors `UsageEvent`'s natural-key dedupe): a
    replay of an already-recorded id returns the EXISTING row and `False` — no second
    row, no second transition. On a genuinely new id:

    - `result == "pass"`  -> `tenant.onboarding_state = "conectado"`,
      `connected_at = now`, blocker cleared, `next_retry_at = None`.
    - `result == "fail"`  -> resolve the blocker via `map_error_to_blocker(error_code,
      blocker_reason)`; `onboarding_state` becomes `"aguardando_acao_manual"` when that
      blocker requires manual action, else `"aguardando_elegibilidade"`.

    Guard: a `"fail"` attempt on a tenant that is already `"ativo"` or `"conectado"`
    must never regress it — the row is still recorded (so the attempt is auditable) but
    the tenant's own state/blocker are left untouched (log only).

    `blocker_reason` here is the caller's DECLARED/fallback blocker (e.g. the tenant's
    current one) — the "declared" argument `map_error_to_blocker` falls back to when the
    error text itself carries no specific signal.

    Caller commits — this function only `session.add`s the new row and mutates `tenant`.
    """
    from brain_api.models.signup_attempt import SignupAttempt

    existing = await session.get(SignupAttempt, attempt_id)
    if existing is not None:
        return existing, False

    now = datetime.now(UTC)
    row = SignupAttempt(
        id=attempt_id,
        tenant_id=tenant.id,
        source=source,
        day_offset=day_offset,
        result=result,
        error_code=error_code,
    )

    if result == ATTEMPT_RESULT_PASS:
        row.blocker_reason = None
        tenant.onboarding_state = STATE_CONECTADO
        tenant.connected_at = now
        tenant.blocker_reason = None
        tenant.next_retry_at = None
    elif result == ATTEMPT_RESULT_FAIL:
        resolved_blocker = map_error_to_blocker(error_code, blocker_reason)
        row.blocker_reason = resolved_blocker
        if tenant.onboarding_state in (STATE_ATIVO, STATE_CONECTADO):
            logger.info(
                "onboarding_attempt_fail_ignored_no_regress",
                tenant_id=str(tenant.id),
                onboarding_state=tenant.onboarding_state,
                attempt_id=str(attempt_id),
            )
        else:
            tenant.blocker_reason = resolved_blocker
            tenant.onboarding_state = (
                STATE_AGUARDANDO_ACAO_MANUAL
                if resolved_blocker in _MANUAL_ACTION_BLOCKERS
                else STATE_AGUARDANDO_ELEGIBILIDADE
            )

    session.add(row)
    return row, True


# --- blocker resolution + config-status mapping (pure tenant mutations) -----------------


def resolve_blocker(tenant: Tenant) -> bool:
    """Re-enter `aquecimento` from a manual blocker, clearing it (CONTRACT §8).

    Applies from `aguardando_acao_manual`, or from any other state that still carries a
    `blocker_reason` — EXCEPT `conectado`/`ativo`, which are never touched. Returns
    whether a change was applied (`False` is a safe no-op for an already-clean tenant).
    """
    if tenant.onboarding_state in (STATE_CONECTADO, STATE_ATIVO):
        return False
    if tenant.onboarding_state != STATE_AGUARDANDO_ACAO_MANUAL and tenant.blocker_reason is None:
        return False

    tenant.onboarding_state = STATE_AQUECIMENTO
    tenant.blocker_reason = None
    tenant.next_retry_at = datetime.now(UTC) + timedelta(days=3)
    return True


def apply_config_status(tenant: Tenant, *, connected: bool, config_complete: bool) -> str:
    """Pure (connected, config_complete) -> config_status mapping (CONTRACT §8).

    `config_complete AND connected` -> "completa"; `config_complete AND NOT connected`
    -> "configurado_aguardando_numero"; anything else (config incomplete) -> "incompleta".
    Assigns `tenant.config_status` and returns the same value.
    """
    if config_complete and connected:
        value = CONFIG_STATUS_COMPLETA
    elif config_complete and not connected:
        value = CONFIG_STATUS_CONFIGURADO_AGUARDANDO_NUMERO
    else:
        value = CONFIG_STATUS_INCOMPLETA
    tenant.config_status = value
    return value


# --- provisioning defaults (called once, at tenant creation) ----------------------------


def provision_defaults(tenant: Tenant, intake: dict | None, now: datetime) -> None:
    """Seed a freshly-created tenant's onboarding fields (called by
    `services.signup.provision_tenant_from_intent` before its commit).

    Derives the initial state/blocker from `intake` (`derive_initial_state`), anchors
    both the retry and config-reminder clocks at `now`, and — only when the derived
    state is one that actually waits on a retry (`aquecimento` or
    `aguardando_elegibilidade`; a manual-action blocker has no retry countdown yet) —
    calibrates `next_retry_at` via `initial_next_retry_at`.
    """
    state, blocker = derive_initial_state(intake)
    tenant.onboarding_state = state
    tenant.blocker_reason = blocker
    tenant.onboarding_anchor_at = now
    tenant.config_reminder_anchor_at = now
    if state in (STATE_AQUECIMENTO, STATE_AGUARDANDO_ELEGIBILIDADE):
        tenant.next_retry_at = initial_next_retry_at(intake, now)
