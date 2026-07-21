"""services/onboarding.py — pure state-machine logic (STATE-MACHINE + AUTH build).

Ground truth: src/brain_api/services/onboarding.py, CONTRACT_onboarding_v1.md §1/§8.
Every function there is PURE (no HTTP, no secretaria client) — these tests exercise the
module directly, without going through the (not-yet-built) `/doctor/onboarding*` HTTP
surface. `record_attempt` needs a real `Tenant` row (for the FK) and an `AsyncSession`;
the `db_session` fixture (tests/conftest.py) provides a bare in-memory SQLite session.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.models import SignupAttempt, Tenant
from brain_api.services import onboarding


async def _new_tenant(db_session: AsyncSession, **overrides) -> Tenant:
    tenant = Tenant(clinic_name="Test Clinic")
    for key, value in overrides.items():
        setattr(tenant, key, value)
    db_session.add(tenant)
    await db_session.flush()  # assign tenant.id
    return tenant


# --- enum constants: exact-string regression against CONTRACT_onboarding_v1.md §1 ------


def test_enum_constants_match_contract_exact_strings():
    assert onboarding.ONBOARDING_STATES == (
        "pending",
        "aquecimento",
        "aguardando_elegibilidade",
        "aguardando_acao_manual",
        "conectado",
        "ativo",
    )
    assert onboarding.BLOCKER_REASONS == (
        "atividade_insuficiente",
        "numero_em_outro_bsp",
        "sem_acesso_admin_waba",
        "sem_pagina_facebook",
        "outro",
    )
    assert onboarding.CONFIG_STATUSES == (
        "incompleta",
        "configurado_aguardando_numero",
        "completa",
    )
    assert onboarding.ATTEMPT_RESULTS == ("pass", "fail")
    assert onboarding.ATTEMPT_SOURCES == ("user", "retry_nudge", "intake")


# --- derive_initial_state ---------------------------------------------------------------


def test_derive_initial_state_none_intake():
    assert onboarding.derive_initial_state(None) == (onboarding.STATE_AQUECIMENTO, None)


def test_derive_initial_state_empty_intake():
    assert onboarding.derive_initial_state({}) == (onboarding.STATE_AQUECIMENTO, None)


def test_derive_initial_state_prior_api_yes():
    assert onboarding.derive_initial_state({"prior_api": "yes"}) == (
        onboarding.STATE_AGUARDANDO_ACAO_MANUAL,
        onboarding.BLOCKER_NUMERO_EM_OUTRO_BSP,
    )


def test_derive_initial_state_fb_page_yes_unknown_admin():
    assert onboarding.derive_initial_state({"fb_page": "yes_unknown_admin"}) == (
        onboarding.STATE_AGUARDANDO_ACAO_MANUAL,
        onboarding.BLOCKER_SEM_ACESSO_ADMIN_WABA,
    )


def test_derive_initial_state_fb_page_no():
    assert onboarding.derive_initial_state({"fb_page": "no"}) == (
        onboarding.STATE_AQUECIMENTO,
        onboarding.BLOCKER_SEM_PAGINA_FACEBOOK,
    )


def test_derive_initial_state_all_clear():
    intake = {"whatsapp_usage": "business_7d_plus", "prior_api": "no", "fb_page": "yes_admin"}
    assert onboarding.derive_initial_state(intake) == (onboarding.STATE_AQUECIMENTO, None)


def test_derive_initial_state_priority_prior_api_wins_over_fb_page():
    """prior_api=='yes' takes priority even when fb_page ALSO signals a blocker."""
    intake = {"prior_api": "yes", "fb_page": "no"}
    assert onboarding.derive_initial_state(intake) == (
        onboarding.STATE_AGUARDANDO_ACAO_MANUAL,
        onboarding.BLOCKER_NUMERO_EM_OUTRO_BSP,
    )


def test_derive_initial_state_priority_yes_unknown_admin_wins_over_no():
    intake = {"fb_page": "yes_unknown_admin"}
    # Sanity: "no" branch must not also fire for a different fb_page value.
    assert onboarding.derive_initial_state(intake)[1] == onboarding.BLOCKER_SEM_ACESSO_ADMIN_WABA


# --- initial_next_retry_at ---------------------------------------------------------------


def test_initial_next_retry_at_business_7d_plus_is_now():
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
    assert onboarding.initial_next_retry_at({"whatsapp_usage": "business_7d_plus"}, now) == now


def test_initial_next_retry_at_other_usage_is_plus_3_days():
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
    expected = now + timedelta(days=3)
    assert onboarding.initial_next_retry_at({"whatsapp_usage": "business_recent"}, now) == expected
    assert onboarding.initial_next_retry_at({"whatsapp_usage": "none"}, now) == expected


def test_initial_next_retry_at_none_intake_is_plus_3_days():
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
    assert onboarding.initial_next_retry_at(None, now) == now + timedelta(days=3)


def test_initial_next_retry_at_empty_intake_is_plus_3_days():
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
    assert onboarding.initial_next_retry_at({}, now) == now + timedelta(days=3)


# --- map_error_to_blocker -----------------------------------------------------------------


def test_map_error_to_blocker_previously():
    assert (
        onboarding.map_error_to_blocker("Number registered previously", None)
        == onboarding.BLOCKER_NUMERO_EM_OUTRO_BSP
    )


def test_map_error_to_blocker_another_case_insensitive():
    assert (
        onboarding.map_error_to_blocker("REGISTERED BY ANOTHER BSP", None)
        == onboarding.BLOCKER_NUMERO_EM_OUTRO_BSP
    )


def test_map_error_to_blocker_in_use():
    assert (
        onboarding.map_error_to_blocker("phone number already in use", None)
        == onboarding.BLOCKER_NUMERO_EM_OUTRO_BSP
    )


def test_map_error_to_blocker_page():
    assert (
        onboarding.map_error_to_blocker("no Facebook Page found", None)
        == onboarding.BLOCKER_SEM_ACESSO_ADMIN_WABA
    )


def test_map_error_to_blocker_permission():
    assert (
        onboarding.map_error_to_blocker("missing permission scope", None)
        == onboarding.BLOCKER_SEM_ACESSO_ADMIN_WABA
    )


def test_map_error_to_blocker_admin_case_insensitive():
    assert (
        onboarding.map_error_to_blocker("user is not an ADMIN of the business", None)
        == onboarding.BLOCKER_SEM_ACESSO_ADMIN_WABA
    )


def test_map_error_to_blocker_numero_priority_over_admin():
    """Both hint groups present -> the numero_em_outro_bsp check wins (checked first)."""
    assert (
        onboarding.map_error_to_blocker("number previously used, admin issue too", None)
        == onboarding.BLOCKER_NUMERO_EM_OUTRO_BSP
    )


def test_map_error_to_blocker_falls_back_to_valid_declared():
    assert (
        onboarding.map_error_to_blocker("some unrelated failure", "atividade_insuficiente")
        == onboarding.BLOCKER_ATIVIDADE_INSUFICIENTE
    )


def test_map_error_to_blocker_falls_back_to_outro_when_declared_invalid():
    assert onboarding.map_error_to_blocker("some unrelated failure", "not_a_real_blocker") == (
        onboarding.BLOCKER_OUTRO
    )


def test_map_error_to_blocker_none_error_and_none_declared_is_outro():
    assert onboarding.map_error_to_blocker(None, None) == onboarding.BLOCKER_OUTRO


def test_map_error_to_blocker_empty_error_code_falls_back_to_declared():
    assert (
        onboarding.map_error_to_blocker("", onboarding.BLOCKER_SEM_PAGINA_FACEBOOK)
        == onboarding.BLOCKER_SEM_PAGINA_FACEBOOK
    )


# --- record_attempt: pass path -----------------------------------------------------------


async def test_record_attempt_pass_transitions_to_conectado(db_session):
    tenant = await _new_tenant(
        db_session,
        onboarding_state=onboarding.STATE_AQUECIMENTO,
        blocker_reason=onboarding.BLOCKER_ATIVIDADE_INSUFICIENTE,
        next_retry_at=datetime.now(UTC),
    )
    before = datetime.now(UTC)
    attempt_id = uuid4()
    row, is_new = await onboarding.record_attempt(
        db_session,
        tenant,
        attempt_id=attempt_id,
        source=onboarding.ATTEMPT_SOURCE_USER,
        result=onboarding.ATTEMPT_RESULT_PASS,
        blocker_reason=None,
        error_code=None,
        day_offset=None,
    )
    after = datetime.now(UTC)

    assert is_new is True
    assert row.id == attempt_id
    assert row.result == onboarding.ATTEMPT_RESULT_PASS
    assert row.blocker_reason is None
    assert tenant.onboarding_state == onboarding.STATE_CONECTADO
    assert tenant.blocker_reason is None
    assert tenant.next_retry_at is None
    assert before <= tenant.connected_at <= after


# --- record_attempt: fail path ------------------------------------------------------------


async def test_record_attempt_fail_manual_action_blocker(db_session):
    tenant = await _new_tenant(db_session, onboarding_state=onboarding.STATE_AQUECIMENTO)
    row, is_new = await onboarding.record_attempt(
        db_session,
        tenant,
        attempt_id=uuid4(),
        source=onboarding.ATTEMPT_SOURCE_USER,
        result=onboarding.ATTEMPT_RESULT_FAIL,
        blocker_reason=None,
        error_code="this number was registered previously elsewhere",
        day_offset=1,
    )
    assert is_new is True
    assert row.blocker_reason == onboarding.BLOCKER_NUMERO_EM_OUTRO_BSP
    assert tenant.onboarding_state == onboarding.STATE_AGUARDANDO_ACAO_MANUAL
    assert tenant.blocker_reason == onboarding.BLOCKER_NUMERO_EM_OUTRO_BSP


async def test_record_attempt_fail_non_manual_blocker_goes_to_elegibilidade(db_session):
    tenant = await _new_tenant(db_session, onboarding_state=onboarding.STATE_AQUECIMENTO)
    row, is_new = await onboarding.record_attempt(
        db_session,
        tenant,
        attempt_id=uuid4(),
        source=onboarding.ATTEMPT_SOURCE_RETRY_NUDGE,
        result=onboarding.ATTEMPT_RESULT_FAIL,
        blocker_reason=onboarding.BLOCKER_ATIVIDADE_INSUFICIENTE,
        error_code=None,
        day_offset=3,
    )
    assert is_new is True
    assert row.blocker_reason == onboarding.BLOCKER_ATIVIDADE_INSUFICIENTE
    assert tenant.onboarding_state == onboarding.STATE_AGUARDANDO_ELEGIBILIDADE
    assert tenant.blocker_reason == onboarding.BLOCKER_ATIVIDADE_INSUFICIENTE


# --- record_attempt: idempotency ----------------------------------------------------------


async def test_record_attempt_idempotent_replay_no_state_change(db_session):
    tenant = await _new_tenant(db_session, onboarding_state=onboarding.STATE_AQUECIMENTO)
    attempt_id = uuid4()

    first_row, first_is_new = await onboarding.record_attempt(
        db_session,
        tenant,
        attempt_id=attempt_id,
        source=onboarding.ATTEMPT_SOURCE_USER,
        result=onboarding.ATTEMPT_RESULT_PASS,
        blocker_reason=None,
        error_code=None,
        day_offset=None,
    )
    assert first_is_new is True
    state_after_first = tenant.onboarding_state
    connected_at_after_first = tenant.connected_at

    # Replay the SAME attempt_id with a DIFFERENT (contradictory) result — must be a
    # pure no-op on the tenant: no second transition, existing row returned unchanged.
    second_row, second_is_new = await onboarding.record_attempt(
        db_session,
        tenant,
        attempt_id=attempt_id,
        source=onboarding.ATTEMPT_SOURCE_USER,
        result=onboarding.ATTEMPT_RESULT_FAIL,
        blocker_reason=None,
        error_code="admin permission missing",
        day_offset=None,
    )

    assert second_is_new is False
    assert second_row is first_row
    assert second_row.result == onboarding.ATTEMPT_RESULT_PASS  # unchanged from the first write
    assert tenant.onboarding_state == state_after_first
    assert tenant.connected_at == connected_at_after_first


async def test_record_attempt_caller_must_commit(db_session):
    """`record_attempt` never commits — a rollback after it must undo everything."""
    tenant = await _new_tenant(db_session)
    attempt_id = uuid4()
    _, is_new = await onboarding.record_attempt(
        db_session,
        tenant,
        attempt_id=attempt_id,
        source=onboarding.ATTEMPT_SOURCE_USER,
        result=onboarding.ATTEMPT_RESULT_PASS,
        blocker_reason=None,
        error_code=None,
        day_offset=None,
    )
    assert is_new is True

    await db_session.rollback()
    assert await db_session.get(SignupAttempt, attempt_id) is None


# --- record_attempt: no-regress guard ------------------------------------------------------


async def test_record_attempt_fail_does_not_regress_ativo(db_session):
    tenant = await _new_tenant(
        db_session, onboarding_state=onboarding.STATE_ATIVO, blocker_reason=None
    )
    row, is_new = await onboarding.record_attempt(
        db_session,
        tenant,
        attempt_id=uuid4(),
        source=onboarding.ATTEMPT_SOURCE_RETRY_NUDGE,
        result=onboarding.ATTEMPT_RESULT_FAIL,
        blocker_reason=None,
        error_code="page permission denied",
        day_offset=7,
    )
    # The attempt IS still recorded (auditable) with its own resolved blocker...
    assert is_new is True
    assert row.result == onboarding.ATTEMPT_RESULT_FAIL
    assert row.blocker_reason == onboarding.BLOCKER_SEM_ACESSO_ADMIN_WABA
    # ...but the tenant itself must NOT regress.
    assert tenant.onboarding_state == onboarding.STATE_ATIVO
    assert tenant.blocker_reason is None


async def test_record_attempt_fail_does_not_regress_conectado(db_session):
    tenant = await _new_tenant(
        db_session, onboarding_state=onboarding.STATE_CONECTADO, blocker_reason=None
    )
    row, is_new = await onboarding.record_attempt(
        db_session,
        tenant,
        attempt_id=uuid4(),
        source=onboarding.ATTEMPT_SOURCE_USER,
        result=onboarding.ATTEMPT_RESULT_FAIL,
        blocker_reason=None,
        error_code="in use by another business",
        day_offset=None,
    )
    assert is_new is True
    assert row.blocker_reason == onboarding.BLOCKER_NUMERO_EM_OUTRO_BSP
    assert tenant.onboarding_state == onboarding.STATE_CONECTADO
    assert tenant.blocker_reason is None


# --- resolve_blocker -----------------------------------------------------------------------


async def test_resolve_blocker_from_aguardando_acao_manual(db_session):
    tenant = await _new_tenant(
        db_session,
        onboarding_state=onboarding.STATE_AGUARDANDO_ACAO_MANUAL,
        blocker_reason=onboarding.BLOCKER_SEM_PAGINA_FACEBOOK,
    )
    before = datetime.now(UTC)
    changed = onboarding.resolve_blocker(tenant)
    after = datetime.now(UTC)

    assert changed is True
    assert tenant.onboarding_state == onboarding.STATE_AQUECIMENTO
    assert tenant.blocker_reason is None
    assert before + timedelta(days=3) <= tenant.next_retry_at <= after + timedelta(days=3)


def test_resolve_blocker_from_other_state_with_blocker_set():
    tenant = Tenant(
        clinic_name="X",
        onboarding_state=onboarding.STATE_AGUARDANDO_ELEGIBILIDADE,
        blocker_reason=onboarding.BLOCKER_ATIVIDADE_INSUFICIENTE,
    )
    changed = onboarding.resolve_blocker(tenant)
    assert changed is True
    assert tenant.onboarding_state == onboarding.STATE_AQUECIMENTO
    assert tenant.blocker_reason is None


def test_resolve_blocker_noop_when_no_blocker_and_not_manual():
    tenant = Tenant(
        clinic_name="X", onboarding_state=onboarding.STATE_AQUECIMENTO, blocker_reason=None
    )
    changed = onboarding.resolve_blocker(tenant)
    assert changed is False
    assert tenant.onboarding_state == onboarding.STATE_AQUECIMENTO


def test_resolve_blocker_never_touches_conectado():
    tenant = Tenant(
        clinic_name="X",
        onboarding_state=onboarding.STATE_CONECTADO,
        blocker_reason=onboarding.BLOCKER_OUTRO,  # artificial, to prove the guard fires first
    )
    changed = onboarding.resolve_blocker(tenant)
    assert changed is False
    assert tenant.onboarding_state == onboarding.STATE_CONECTADO
    assert tenant.blocker_reason == onboarding.BLOCKER_OUTRO


def test_resolve_blocker_never_touches_ativo():
    tenant = Tenant(
        clinic_name="X",
        onboarding_state=onboarding.STATE_ATIVO,
        blocker_reason=onboarding.BLOCKER_OUTRO,
    )
    changed = onboarding.resolve_blocker(tenant)
    assert changed is False
    assert tenant.onboarding_state == onboarding.STATE_ATIVO
    assert tenant.blocker_reason == onboarding.BLOCKER_OUTRO


# --- apply_config_status --------------------------------------------------------------------


def test_apply_config_status_connected_and_complete():
    tenant = Tenant(clinic_name="X")
    result = onboarding.apply_config_status(tenant, connected=True, config_complete=True)
    assert result == onboarding.CONFIG_STATUS_COMPLETA
    assert tenant.config_status == onboarding.CONFIG_STATUS_COMPLETA


def test_apply_config_status_complete_but_not_connected():
    tenant = Tenant(clinic_name="X")
    result = onboarding.apply_config_status(tenant, connected=False, config_complete=True)
    assert result == onboarding.CONFIG_STATUS_CONFIGURADO_AGUARDANDO_NUMERO
    assert tenant.config_status == onboarding.CONFIG_STATUS_CONFIGURADO_AGUARDANDO_NUMERO


def test_apply_config_status_incomplete_and_connected():
    tenant = Tenant(clinic_name="X")
    result = onboarding.apply_config_status(tenant, connected=True, config_complete=False)
    assert result == onboarding.CONFIG_STATUS_INCOMPLETA
    assert tenant.config_status == onboarding.CONFIG_STATUS_INCOMPLETA


def test_apply_config_status_incomplete_and_not_connected():
    tenant = Tenant(clinic_name="X")
    result = onboarding.apply_config_status(tenant, connected=False, config_complete=False)
    assert result == onboarding.CONFIG_STATUS_INCOMPLETA
    assert tenant.config_status == onboarding.CONFIG_STATUS_INCOMPLETA


# --- provision_defaults ----------------------------------------------------------------------


def test_provision_defaults_none_intake():
    tenant = Tenant(clinic_name="X")
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
    onboarding.provision_defaults(tenant, None, now)

    assert tenant.onboarding_state == onboarding.STATE_AQUECIMENTO
    assert tenant.blocker_reason is None
    assert tenant.onboarding_anchor_at == now
    assert tenant.config_reminder_anchor_at == now
    # aquecimento IS retry-eligible -> next_retry_at gets calibrated (no intake -> +3d).
    assert tenant.next_retry_at == now + timedelta(days=3)


def test_provision_defaults_business_7d_plus_retries_immediately():
    tenant = Tenant(clinic_name="X")
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
    onboarding.provision_defaults(tenant, {"whatsapp_usage": "business_7d_plus"}, now)

    assert tenant.onboarding_state == onboarding.STATE_AQUECIMENTO
    assert tenant.next_retry_at == now


def test_provision_defaults_manual_action_blocker_leaves_next_retry_at_unset():
    tenant = Tenant(clinic_name="X")
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
    onboarding.provision_defaults(tenant, {"prior_api": "yes"}, now)

    assert tenant.onboarding_state == onboarding.STATE_AGUARDANDO_ACAO_MANUAL
    assert tenant.blocker_reason == onboarding.BLOCKER_NUMERO_EM_OUTRO_BSP
    assert tenant.onboarding_anchor_at == now
    assert tenant.config_reminder_anchor_at == now
    # No retry countdown for a manual-action blocker.
    assert tenant.next_retry_at is None


def test_provision_defaults_sem_pagina_facebook():
    tenant = Tenant(clinic_name="X")
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
    onboarding.provision_defaults(tenant, {"fb_page": "no"}, now)

    assert tenant.onboarding_state == onboarding.STATE_AQUECIMENTO
    assert tenant.blocker_reason == onboarding.BLOCKER_SEM_PAGINA_FACEBOOK
    assert tenant.next_retry_at == now + timedelta(days=3)
