"""Onboarding endpoints + integration vertical tests (ENDPOINTS + INTEGRATION + BILLING
build, CONTRACT_onboarding_v1.md §4-§8).

Ground truth: services/secretaria_provisioning.py, services/meta_graph.py,
services/onboarding_sync.py, api/onboarding.py, api/internal.py (the two new onboarding
routes), api/auth.py (exchange-invite-token). Reuses the seeded in-memory app + tenant
fixtures from tests/test_rbac.py (Tenant A/B start in the DEFAULT onboarding state:
`pending`/`incompleta`, no anchors — the Tenant model's own column defaults, since the
fixture never sets onboarding fields explicitly).

Two mocking layers, both "monkeypatched outbound HTTP" per the existing convention:
- A dedicated fake-httpx section for `services/secretaria_provisioning.py` (mirrors
  `tests/test_doctor_secretaria.py`'s pattern for `services/secretaria_internal.py`) and
  for `services/meta_graph.py`, exercising the real client/config-gate/status-mapping
  logic with no network.
- For the HTTP router tests, the higher-level service FUNCTIONS
  (`secretaria_provisioning.*`, `meta_graph.exchange_code_for_token`) are monkeypatched
  directly — the same boundary, just skipping the httpx plumbing already covered above,
  so each router test can focus on ROUTER decisions (which is exactly the same style
  test_billing.py uses for its Stripe fake and test_usage_events.py for its pair-key gate).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from brain_api.services import meta_graph, onboarding_sync, secretaria_provisioning
from tests.test_rbac import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    CLINIC_A,
    CLINIC_B,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    OWNER_A_PROFESSIONAL_ID,
    OWNER_B_EMAIL,
    OWNER_B_PASSWORD,
    _bearer,
    _token,
)


@pytest.fixture(autouse=True)
def _clear_config_status_cache():
    """`services.onboarding_sync._config_status_cache` is a module-level (per-process)
    TTL cache keyed by tenant id. Tenant ids are fresh UUID4s per test (the `client`
    fixture creates new rows every time), so cross-test bleed is not realistically
    possible — but clearing it defensively keeps each test's throttle assertions
    self-contained and easy to reason about."""
    onboarding_sync._config_status_cache.clear()
    yield
    onboarding_sync._config_status_cache.clear()


async def _tenant_ids(client, admin_token: str) -> dict[str, str]:
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    return {t["clinic_name"]: t["id"] for t in tenants}


async def _create_staff(client, admin_token: str, tenant_id: str, email: str) -> None:
    """A plain (non-owner, non-manager) doctor — the role-taxonomy successor of the old
    `tenant_staff` role. `is_owner`/`is_manager` default False, so `require_owner`-gated
    routes (the pause switch) still reject this user, same as the old `tenant_staff`."""
    resp = await client.post(
        "/admin/users",
        headers=_bearer(admin_token),
        json={
            "email": email,
            "name": "Staff Member",
            "password": "staffpass1",
            "role": "doctor",
            "tenant_id": tenant_id,
        },
    )
    assert resp.status_code == 201, resp.text


# ===========================================================================================
# services/secretaria_provisioning.py — dedicated client tests (fake httpx, no network)
# ===========================================================================================

CONFIGURED = SimpleNamespace(
    SECRETARIA_BASE_URL="http://secretaria:8000",
    SECRETARIA_API_KEY="match-key",
    SECRETARIA_TIMEOUT_SECONDS=10.0,
)


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


def _install_fake_provisioning_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: _FakeResponse | None = None,
    exc: Exception | None = None,
    configured: SimpleNamespace = CONFIGURED,
) -> dict:
    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def request(self, method: str, path: str, headers=None, json=None, params=None):
            captured["method"] = method
            captured["path"] = path
            captured["headers"] = headers
            captured["json"] = json
            captured["params"] = params
            if exc is not None:
                raise exc
            assert response is not None
            return response

    monkeypatch.setattr(secretaria_provisioning, "get_settings", lambda: configured)
    monkeypatch.setattr(secretaria_provisioning.httpx, "AsyncClient", _FakeClient)
    return captured


async def test_provisioning_unconfigured_mesh_degrades_no_network(monkeypatch):
    unconfigured = SimpleNamespace(
        SECRETARIA_BASE_URL="", SECRETARIA_API_KEY="", SECRETARIA_TIMEOUT_SECONDS=10.0
    )
    monkeypatch.setattr(secretaria_provisioning, "get_settings", lambda: unconfigured)

    def _boom(*a, **kw):  # pragma: no cover - proves no network call is attempted.
        raise AssertionError("must not attempt a network call when unconfigured")

    monkeypatch.setattr(secretaria_provisioning.httpx, "AsyncClient", _boom)

    assert (
        await secretaria_provisioning.provision_tenant(
            uuid4(), clinic_name="X", contact_email=None, timezone="America/Sao_Paulo"
        )
        is False
    )
    assert (
        await secretaria_provisioning.connect_whatsapp(
            uuid4(), phone_number_id="123", waba_id=None, access_token=None
        )
        == secretaria_provisioning.CONNECTION_ERROR
    )
    assert await secretaria_provisioning.get_config_status(uuid4()) is None
    assert (
        await secretaria_provisioning.create_professional(
            uuid4(), name="Dr. X", specialty=None, about=None
        )
        is None
    )
    assert await secretaria_provisioning.activate_tenant(uuid4()) is None
    assert await secretaria_provisioning.send_notification_email("a@b.com", "t", {}) is False


async def test_provision_tenant_success_sends_expected_body(monkeypatch):
    captured = _install_fake_provisioning_httpx(
        monkeypatch, response=_FakeResponse(200, {"tenant_id": "x", "created": True})
    )
    tenant_id = uuid4()
    ok = await secretaria_provisioning.provision_tenant(
        tenant_id, clinic_name="Clinica X", contact_email="dr@x.com", timezone="America/Sao_Paulo"
    )
    assert ok is True
    assert captured["method"] == "POST"
    assert captured["path"] == "/internal/tenants"
    assert captured["headers"]["X-Internal-Api-Key"] == "match-key"
    assert captured["json"] == {
        "tenant_id": str(tenant_id),
        "clinic_name": "Clinica X",
        "contact_email": "dr@x.com",
        "timezone": "America/Sao_Paulo",
    }


async def test_connect_whatsapp_ok_conflict_and_error_status_mapping(monkeypatch):
    for status_code, expected in (
        (200, secretaria_provisioning.CONNECTION_OK),
        (409, secretaria_provisioning.CONNECTION_CONFLICT),
        (500, secretaria_provisioning.CONNECTION_ERROR),
        (404, secretaria_provisioning.CONNECTION_ERROR),
    ):
        _install_fake_provisioning_httpx(
            monkeypatch, response=_FakeResponse(status_code, {"detail": "whatever"})
        )
        outcome = await secretaria_provisioning.connect_whatsapp(
            uuid4(), phone_number_id="5511999999999", waba_id="waba1", access_token="tok"
        )
        assert outcome == expected, f"status {status_code} -> expected {expected}"


async def test_connect_whatsapp_network_error_is_connection_error(monkeypatch):
    _install_fake_provisioning_httpx(monkeypatch, exc=httpx.ConnectError("boom"))
    outcome = await secretaria_provisioning.connect_whatsapp(
        uuid4(), phone_number_id="123", waba_id=None, access_token=None
    )
    assert outcome == secretaria_provisioning.CONNECTION_ERROR


async def test_upstream_key_mismatch_and_unconfigured_both_fold_to_none(monkeypatch):
    # 401 = key mismatch (both sides set but different).
    _install_fake_provisioning_httpx(monkeypatch, response=_FakeResponse(401, {"detail": "no"}))
    assert await secretaria_provisioning.get_config_status(uuid4()) is None
    # 403 = secretaria's OWN key is unconfigured server-side.
    _install_fake_provisioning_httpx(monkeypatch, response=_FakeResponse(403, {"detail": "no"}))
    assert await secretaria_provisioning.get_config_status(uuid4()) is None


async def test_get_config_status_returns_parsed_payload(monkeypatch):
    payload = {
        "connected": True,
        "config_complete": True,
        "mode_resolved": False,
        "professionals": [{"id": "p1", "name": "Dr. X", "complete": True}],
    }
    _install_fake_provisioning_httpx(monkeypatch, response=_FakeResponse(200, payload))
    assert await secretaria_provisioning.get_config_status(uuid4()) == payload


async def test_create_professional_and_activate_and_email(monkeypatch):
    captured = _install_fake_provisioning_httpx(
        monkeypatch, response=_FakeResponse(200, {"professional_id": "p1", "created": True})
    )
    result = await secretaria_provisioning.create_professional(
        uuid4(), name="Dr. X", specialty="Cardio", about=None
    )
    assert result == {"professional_id": "p1", "created": True}
    assert captured["json"] == {"name": "Dr. X", "specialty": "Cardio", "about": None}

    _install_fake_provisioning_httpx(
        monkeypatch, response=_FakeResponse(200, {"activated": True, "reasons": []})
    )
    assert await secretaria_provisioning.activate_tenant(uuid4()) == {
        "activated": True,
        "reasons": [],
    }

    _install_fake_provisioning_httpx(monkeypatch, response=_FakeResponse(200, {"queued": True}))
    assert await secretaria_provisioning.send_notification_email("a@b.com", "t", {}) is True

    _install_fake_provisioning_httpx(monkeypatch, response=_FakeResponse(500, {}))
    assert await secretaria_provisioning.send_notification_email("a@b.com", "t", {}) is False


# ===========================================================================================
# services/meta_graph.py — dedicated client tests (fake httpx, no network)
# ===========================================================================================


def _install_fake_meta_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: _FakeResponse | None = None,
    exc: Exception | None = None,
) -> dict:
    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def get(self, path: str, params=None):
            captured["path"] = path
            captured["params"] = params
            if exc is not None:
                raise exc
            assert response is not None
            return response

    monkeypatch.setattr(
        meta_graph,
        "get_settings",
        lambda: SimpleNamespace(
            META_APP_ID="app123",
            META_APP_SECRET="secret123",
            META_GRAPH_BASE_URL="https://graph.test/v99",
        ),
    )
    monkeypatch.setattr(meta_graph.httpx, "AsyncClient", _FakeClient)
    return captured


async def test_meta_exchange_unconfigured_returns_none(monkeypatch):
    monkeypatch.setattr(
        meta_graph, "get_settings", lambda: SimpleNamespace(META_APP_ID="", META_APP_SECRET="")
    )
    assert await meta_graph.exchange_code_for_token("code123") is None


async def test_meta_exchange_success(monkeypatch):
    captured = _install_fake_meta_httpx(
        monkeypatch, response=_FakeResponse(200, {"access_token": "tok_abc"})
    )
    token = await meta_graph.exchange_code_for_token("code123")
    assert token == "tok_abc"
    assert captured["path"] == "/oauth/access_token"
    assert captured["params"] == {
        "client_id": "app123",
        "client_secret": "secret123",
        "code": "code123",
    }


async def test_meta_exchange_non_200_returns_none(monkeypatch):
    _install_fake_meta_httpx(monkeypatch, response=_FakeResponse(400, {"error": "bad_code"}))
    assert await meta_graph.exchange_code_for_token("bad") is None


async def test_meta_exchange_malformed_body_returns_none(monkeypatch):
    _install_fake_meta_httpx(monkeypatch, response=_FakeResponse(200, {"no_token_here": True}))
    assert await meta_graph.exchange_code_for_token("code") is None


async def test_meta_exchange_network_error_returns_none(monkeypatch):
    _install_fake_meta_httpx(monkeypatch, exc=httpx.ConnectError("boom"))
    assert await meta_graph.exchange_code_for_token("code") is None


# ===========================================================================================
# services/meta_graph.py — subscribe_app_to_waba (Task 3, Coexistence onboarding)
# ===========================================================================================


def _install_fake_waba_subscribe_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: _FakeResponse | None = None,
    exc: Exception | None = None,
) -> dict:
    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def post(self, path: str, headers=None):
            captured["path"] = path
            captured["headers"] = headers
            if exc is not None:
                raise exc
            assert response is not None
            return response

    monkeypatch.setattr(
        meta_graph,
        "get_settings",
        lambda: SimpleNamespace(META_GRAPH_BASE_URL="https://graph.test/v99"),
    )
    monkeypatch.setattr(meta_graph.httpx, "AsyncClient", _FakeClient)
    return captured


async def test_subscribe_app_to_waba_success(monkeypatch):
    """(b) subscribe_app_to_waba success — hits the right path, Bearer header only, no
    token in a query param."""
    captured = _install_fake_waba_subscribe_httpx(
        monkeypatch, response=_FakeResponse(200, {"success": True})
    )
    ok = await meta_graph.subscribe_app_to_waba("waba_123", "tok_secret")
    assert ok is True
    assert captured["path"] == "/waba_123/subscribed_apps"
    assert captured["headers"] == {"Authorization": "Bearer tok_secret"}


async def test_subscribe_app_to_waba_non_200_returns_false(monkeypatch):
    """(e) the token never appears in a log call — the warning carries only
    upstream_status, never the access_token value. structlog's `PrintLoggerFactory`
    doesn't route through stdlib logging (so `caplog` can't see it) — assert directly on
    the args/kwargs the logger was called with instead."""
    _install_fake_waba_subscribe_httpx(monkeypatch, response=_FakeResponse(403, {"error": "no"}))

    logged_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        meta_graph.logger, "warning", lambda *a, **kw: logged_calls.append((a, kw))
    )

    ok = await meta_graph.subscribe_app_to_waba("waba_123", "tok_secret")
    assert ok is False
    assert logged_calls == [(("meta_waba_subscribe_failed",), {"upstream_status": 403})]
    assert "tok_secret" not in repr(logged_calls)


async def test_subscribe_app_to_waba_network_error_returns_false(monkeypatch):
    _install_fake_waba_subscribe_httpx(monkeypatch, exc=httpx.ConnectError("boom"))
    ok = await meta_graph.subscribe_app_to_waba("waba_123", "tok_secret")
    assert ok is False


# ===========================================================================================
# Scope A: provisioning bridge (webhook post-commit) + lazy ensure
# ===========================================================================================


async def test_webhook_provisioning_bridge_fires_and_stamps_secretaria_provisioned_at(
    client, monkeypatch
):
    from tests.test_billing import _event, _install_fake_stripe_httpx, _post_webhook
    from tests.test_signup import _create_intent

    calls: list[dict] = []

    async def fake_provision(tenant_id, *, clinic_name, contact_email, timezone):
        calls.append(
            {
                "tenant_id": tenant_id,
                "clinic_name": clinic_name,
                "contact_email": contact_email,
                "timezone": timezone,
            }
        )
        return True

    monkeypatch.setattr(secretaria_provisioning, "provision_tenant", fake_provision)

    intent_id = await _create_intent(
        client, email="bridge.success@example.com", catalog_ids=["secretaria_basico"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_bridge_ok", "url": "https://checkout.stripe.test/b1"}
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200

    obj = {
        "customer": "cus_bridge_ok",
        "subscription": "sub_bridge_ok",
        "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
    }
    resp = await _post_webhook(client, _event("evt_bridge_ok", "checkout.session.completed", obj))
    assert resp.status_code == 200, resp.text

    assert len(calls) == 1
    assert calls[0]["clinic_name"] == "Clinica Signup Test"
    assert calls[0]["timezone"] == "America/Sao_Paulo"
    assert calls[0]["contact_email"] == "bridge.success@example.com"

    # Verify it stuck: the SAME event redelivered must NOT fire the bridge again
    # (already_provisioned guard in services.billing._apply_signup_intent_checkout).
    replay = await _post_webhook(client, _event("evt_bridge_ok", "checkout.session.completed", obj))
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert len(calls) == 1


async def test_webhook_provisioning_bridge_failure_still_acks_webhook(client, monkeypatch):
    """A secretaria outage during the post-commit bridge must NEVER break the webhook
    (CONTRACT_onboarding_v1.md scope A: 'never let a secretaria outage break the
    webhook')."""
    from tests.test_billing import _event, _install_fake_stripe_httpx, _post_webhook
    from tests.test_signup import _create_intent

    async def failing_provision(*args, **kwargs):
        raise RuntimeError("secretaria is down")

    monkeypatch.setattr(secretaria_provisioning, "provision_tenant", failing_provision)

    intent_id = await _create_intent(
        client, email="bridge.failure@example.com", catalog_ids=["secretaria_basico"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_bridge_fail", "url": "https://checkout.stripe.test/b2"}
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200

    obj = {
        "customer": "cus_bridge_fail",
        "subscription": "sub_bridge_fail",
        "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
    }
    resp = await _post_webhook(client, _event("evt_bridge_fail", "checkout.session.completed", obj))
    # The webhook itself still acks 200 despite the bridge blowing up internally.
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"received": True, "duplicate": False}


async def test_lazy_ensure_retries_provisioning_on_get_onboarding(client, monkeypatch):
    """A tenant provisioned entirely via admin tooling never ran the webhook bridge, so
    `secretaria_provisioned_at` is NULL; the first `GET /doctor/onboarding` should retry
    it (fail-soft either way)."""
    calls: list[str] = []

    async def fake_provision(tenant_id, *, clinic_name, contact_email, timezone):
        calls.append(str(tenant_id))
        return True

    monkeypatch.setattr(secretaria_provisioning, "provision_tenant", fake_provision)

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/doctor/onboarding", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["secretaria_provisioned"] is True
    assert len(calls) == 1

    # A second call is now a no-op (already provisioned) — the bridge is not re-fired.
    resp2 = await client.get("/doctor/onboarding", headers=_bearer(token))
    assert resp2.status_code == 200
    assert resp2.json()["secretaria_provisioned"] is True
    assert len(calls) == 1


async def test_lazy_ensure_failure_is_fail_soft(client, monkeypatch):
    async def failing_provision(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(secretaria_provisioning, "provision_tenant", failing_provision)

    token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    resp = await client.get("/doctor/onboarding", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["secretaria_provisioned"] is False


# ===========================================================================================
# GET /doctor/onboarding — shape + throttle
# ===========================================================================================


async def test_get_onboarding_shape_default_state(client, monkeypatch):
    monkeypatch.setattr(secretaria_provisioning, "provision_tenant", _noop_async(True))
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/doctor/onboarding", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "onboarding_state": "pending",
        "blocker_reason": None,
        "config_status": "incompleta",
        "connected": False,
        "mode_resolved": False,
        "secretaria_provisioned": True,
        "next_retry_at": None,
        "retry_paused": False,
        "config_reminder_paused": False,
        "last_attempt": None,
        "embedded_signup": {
            "configured": False,
            "app_id": None,
            "config_id": None,
            # META_ES_COEXISTENCE_FEATURE_TYPE's code default ("whatsapp_business_app_
            # onboarding") is not overridden anywhere in conftest.py, so it flows through.
            "coexistence_feature_type": "whatsapp_business_app_onboarding",
        },
    }


async def test_get_onboarding_embedded_signup_configured_when_both_ids_set(client, monkeypatch):
    import brain_api.api.onboarding as onboarding_api

    monkeypatch.setattr(secretaria_provisioning, "provision_tenant", _noop_async(True))
    monkeypatch.setattr(
        onboarding_api,
        "get_settings",
        lambda: SimpleNamespace(
            META_APP_ID="app1", META_ES_CONFIG_ID="cfg1", META_ES_COEXISTENCE_FEATURE_TYPE=""
        ),
    )
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/doctor/onboarding", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["embedded_signup"] == {
        "configured": True,
        "app_id": "app1",
        "config_id": "cfg1",
        "coexistence_feature_type": None,
    }


async def test_get_onboarding_coexistence_feature_type_set(client, monkeypatch):
    """(a) GET /doctor/onboarding surfaces `coexistence_feature_type` when the setting is
    non-empty — the setting-vs-schema-vs-endpoint wiring, end to end."""
    import brain_api.api.onboarding as onboarding_api

    monkeypatch.setattr(secretaria_provisioning, "provision_tenant", _noop_async(True))
    monkeypatch.setattr(
        onboarding_api,
        "get_settings",
        lambda: SimpleNamespace(
            META_APP_ID="",
            META_ES_CONFIG_ID="",
            META_ES_COEXISTENCE_FEATURE_TYPE="whatsapp_business_app_onboarding",
        ),
    )
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/doctor/onboarding", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert (
        resp.json()["embedded_signup"]["coexistence_feature_type"]
        == "whatsapp_business_app_onboarding"
    )


async def test_get_onboarding_rejects_admin_token(client):
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    resp = await client.get("/doctor/onboarding", headers=_bearer(admin_token))
    assert resp.status_code == 403


async def test_get_onboarding_throttles_config_status_pull(client, monkeypatch):
    monkeypatch.setattr(secretaria_provisioning, "provision_tenant", _noop_async(True))
    calls = {"n": 0}

    async def fake_get_config_status(tenant_id):
        calls["n"] += 1
        return {"connected": False, "config_complete": False, "mode_resolved": False}

    monkeypatch.setattr(secretaria_provisioning, "get_config_status", fake_get_config_status)

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    first = await client.get("/doctor/onboarding", headers=_bearer(token))
    assert first.status_code == 200
    second = await client.get("/doctor/onboarding", headers=_bearer(token))
    assert second.status_code == 200

    # Throttled: the second call within CONFIG_STATUS_PULL_TTL_SECONDS must NOT re-pull.
    assert calls["n"] == 1


# ===========================================================================================
# POST /doctor/onboarding/attempts
# ===========================================================================================


def _noop_async(value):
    async def _f(*args, **kwargs):
        return value

    return _f


async def test_attempt_pass_connects_and_transitions_to_conectado(client, monkeypatch):
    monkeypatch.setattr(secretaria_provisioning, "provision_tenant", _noop_async(True))
    monkeypatch.setattr(secretaria_provisioning, "get_config_status", _noop_async(None))
    email_calls: list[dict] = []

    async def fake_email(to, template, variables):
        email_calls.append({"to": to, "template": template, "variables": variables})
        return True

    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", fake_email)

    connect_calls: list[dict] = []

    async def fake_connect(tenant_id, *, phone_number_id, waba_id, access_token):
        connect_calls.append(
            {"phone_number_id": phone_number_id, "waba_id": waba_id, "access_token": access_token}
        )
        return secretaria_provisioning.CONNECTION_OK

    monkeypatch.setattr(secretaria_provisioning, "connect_whatsapp", fake_connect)

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    attempt_id = str(uuid4())
    resp = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(token),
        json={
            "attempt_id": attempt_id,
            "result": "pass",
            "phone_number_id": "5511999990000",
            "waba_id": "waba_1",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["replayed"] is False
    assert body["onboarding_state"] == "conectado"
    assert body["blocker_reason"] is None
    assert len(connect_calls) == 1
    assert connect_calls[0]["phone_number_id"] == "5511999990000"
    # connection_success email fired (fire-and-forget, to the owner).
    assert len(email_calls) == 1
    assert email_calls[0]["to"] == OWNER_A_EMAIL
    assert email_calls[0]["template"] == "connection_success"

    onboarding_resp = await client.get("/doctor/onboarding", headers=_bearer(token))
    assert onboarding_resp.json()["connected"] is True


async def test_attempt_pass_waba_subscribe_failure_records_fail(client, monkeypatch):
    """(c) a failed subscribe_app_to_waba call folds into a 'fail' attempt with
    error_code='waba_subscribe_failed' — and connect_whatsapp is never reached."""
    import brain_api.api.onboarding as onboarding_api

    monkeypatch.setattr(meta_graph, "exchange_code_for_token", _noop_async("tok_secret_value"))
    monkeypatch.setattr(meta_graph, "subscribe_app_to_waba", _noop_async(False))

    def _must_not_be_called(*a, **kw):  # pragma: no cover - proves the I/O ordering.
        raise AssertionError("connect_whatsapp must not be called after a failed subscribe")

    monkeypatch.setattr(secretaria_provisioning, "connect_whatsapp", _must_not_be_called)

    # (e) the access token must never reach a logger call along this path either.
    logged_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        onboarding_api.logger, "info", lambda *a, **kw: logged_calls.append((a, kw))
    )

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(token),
        json={
            "attempt_id": str(uuid4()),
            "result": "pass",
            "code": "meta_auth_code",
            "phone_number_id": "5511999990000",
            "waba_id": "waba_1",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["onboarding_state"] != "conectado"

    onboarding_resp = await client.get("/doctor/onboarding", headers=_bearer(token))
    last_attempt = onboarding_resp.json()["last_attempt"]
    assert last_attempt["result"] == "fail"
    assert last_attempt["error_code"] == "waba_subscribe_failed"

    assert "tok_secret_value" not in repr(logged_calls)


async def test_attempt_pass_subscribe_success_then_connects(client, monkeypatch):
    """Subscribe succeeds -> connect_whatsapp still runs and the attempt reaches
    'conectado' normally (subscribe is a gate, not a fail-soft no-op)."""
    monkeypatch.setattr(meta_graph, "exchange_code_for_token", _noop_async("tok_secret_value"))

    subscribe_calls: list[dict] = []

    async def fake_subscribe(waba_id, access_token):
        subscribe_calls.append({"waba_id": waba_id, "access_token": access_token})
        return True

    monkeypatch.setattr(meta_graph, "subscribe_app_to_waba", fake_subscribe)
    monkeypatch.setattr(secretaria_provisioning, "get_config_status", _noop_async(None))
    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _noop_async(True))

    connect_calls: list[dict] = []

    async def fake_connect(tenant_id, *, phone_number_id, waba_id, access_token):
        connect_calls.append({"phone_number_id": phone_number_id, "waba_id": waba_id})
        return secretaria_provisioning.CONNECTION_OK

    monkeypatch.setattr(secretaria_provisioning, "connect_whatsapp", fake_connect)

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(token),
        json={
            "attempt_id": str(uuid4()),
            "result": "pass",
            "code": "meta_auth_code",
            "phone_number_id": "5511999990000",
            "waba_id": "waba_1",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["onboarding_state"] == "conectado"
    assert subscribe_calls == [{"waba_id": "waba_1", "access_token": "tok_secret_value"}]
    assert len(connect_calls) == 1


async def test_attempt_pass_missing_waba_id_skips_subscribe(client, monkeypatch):
    """(d) waba_id absent -> subscribe_app_to_waba is never called, and the attempt
    proceeds through connect_whatsapp as before (the pre-existing 'attempt without a
    waba_id' path is preserved)."""
    monkeypatch.setattr(meta_graph, "exchange_code_for_token", _noop_async("tok_secret_value"))

    def _must_not_be_called(*a, **kw):  # pragma: no cover - proves the skip.
        raise AssertionError("subscribe_app_to_waba must not be called without a waba_id")

    monkeypatch.setattr(meta_graph, "subscribe_app_to_waba", _must_not_be_called)
    monkeypatch.setattr(secretaria_provisioning, "get_config_status", _noop_async(None))
    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _noop_async(True))
    monkeypatch.setattr(
        secretaria_provisioning, "connect_whatsapp", _noop_async(secretaria_provisioning.CONNECTION_OK)
    )

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(token),
        json={
            "attempt_id": str(uuid4()),
            "result": "pass",
            "code": "meta_auth_code",
            "phone_number_id": "5511999990000",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["onboarding_state"] == "conectado"


async def test_attempt_pass_requires_phone_number_id(client):
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(token),
        json={"attempt_id": str(uuid4()), "result": "pass"},
    )
    assert resp.status_code == 422


async def test_attempt_pass_token_exchange_failure_records_fail_never_calls_connect(
    client, monkeypatch
):
    monkeypatch.setattr(meta_graph, "exchange_code_for_token", _noop_async(None))

    def _must_not_be_called(*a, **kw):  # pragma: no cover - proves the I/O ordering.
        raise AssertionError("connect_whatsapp must not be called after a failed exchange")

    monkeypatch.setattr(secretaria_provisioning, "connect_whatsapp", _must_not_be_called)

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(token),
        json={
            "attempt_id": str(uuid4()),
            "result": "pass",
            "code": "meta_auth_code",
            "phone_number_id": "5511999990000",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["onboarding_state"] != "conectado"

    onboarding_resp = await client.get("/doctor/onboarding", headers=_bearer(token))
    last_attempt = onboarding_resp.json()["last_attempt"]
    assert last_attempt["result"] == "fail"
    assert last_attempt["error_code"] == "token_exchange_failed"


async def test_attempt_pass_conflict_records_fail_with_phone_number_conflict(client, monkeypatch):
    """Proves connect_whatsapp is called BEFORE record_attempt(pass): a 409 from
    secretaria must never let the tenant reach 'conectado'."""
    monkeypatch.setattr(
        secretaria_provisioning,
        "connect_whatsapp",
        _noop_async(secretaria_provisioning.CONNECTION_CONFLICT),
    )
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(token),
        json={
            "attempt_id": str(uuid4()),
            "result": "pass",
            "phone_number_id": "5511999990000",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["onboarding_state"] != "conectado"

    onboarding_resp = await client.get("/doctor/onboarding", headers=_bearer(token))
    body2 = onboarding_resp.json()
    assert body2["connected"] is False
    assert body2["last_attempt"]["result"] == "fail"
    assert body2["last_attempt"]["error_code"] == "phone_number_conflict"


async def test_attempt_pass_other_connection_error_records_fail(client, monkeypatch):
    monkeypatch.setattr(
        secretaria_provisioning,
        "connect_whatsapp",
        _noop_async(secretaria_provisioning.CONNECTION_ERROR),
    )
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(token),
        json={"attempt_id": str(uuid4()), "result": "pass", "phone_number_id": "5511999990000"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["onboarding_state"] != "conectado"


async def test_attempt_fail_maps_error_code_to_blocker(client):
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(token),
        json={
            "attempt_id": str(uuid4()),
            "result": "fail",
            "error_code": "This number was registered previously",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["onboarding_state"] == "aguardando_acao_manual"
    assert body["blocker_reason"] == "numero_em_outro_bsp"


async def test_attempt_replay_is_idempotent_no_double_io(client, monkeypatch):
    connect_calls = {"n": 0}

    async def fake_connect(tenant_id, *, phone_number_id, waba_id, access_token):
        connect_calls["n"] += 1
        return secretaria_provisioning.CONNECTION_OK

    monkeypatch.setattr(secretaria_provisioning, "connect_whatsapp", fake_connect)
    monkeypatch.setattr(secretaria_provisioning, "get_config_status", _noop_async(None))
    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _noop_async(True))

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    attempt_id = str(uuid4())
    payload = {
        "attempt_id": attempt_id,
        "result": "pass",
        "phone_number_id": "5511999990000",
    }
    first = await client.post("/doctor/onboarding/attempts", headers=_bearer(token), json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False

    second = await client.post("/doctor/onboarding/attempts", headers=_bearer(token), json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["onboarding_state"] == first.json()["onboarding_state"]

    # The replay short-circuited BEFORE any I/O — connect_whatsapp called exactly once.
    assert connect_calls["n"] == 1


async def test_attempt_unknown_doctor_token_rejected(client):
    resp = await client.post(
        "/doctor/onboarding/attempts",
        json={"attempt_id": str(uuid4()), "result": "fail"},
    )
    assert resp.status_code == 401


# ===========================================================================================
# resolve-blocker / pause
# ===========================================================================================


async def test_resolve_blocker_clears_and_reenters_aquecimento(client):
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    fail_resp = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(token),
        json={"attempt_id": str(uuid4()), "result": "fail", "error_code": "some odd failure"},
    )
    assert fail_resp.status_code == 200
    assert fail_resp.json()["onboarding_state"] == "aguardando_elegibilidade"

    resp = await client.post("/doctor/onboarding/resolve-blocker", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["onboarding_state"] == "aquecimento"
    assert body["blocker_reason"] is None


async def test_pause_requires_owner_role(client):
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    staff_email = "pause.staff@a.com"
    await _create_staff(client, admin_token, tenant_a_id, staff_email)
    staff_token = await _token(client, staff_email, "staffpass1")

    resp = await client.post(
        "/doctor/onboarding/pause", headers=_bearer(staff_token), json={"retries": True}
    )
    assert resp.status_code == 403


async def test_pause_owner_sets_kill_switches_independently(client):
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)

    r1 = await client.post(
        "/doctor/onboarding/pause", headers=_bearer(token), json={"retries": True}
    )
    assert r1.status_code == 200, r1.text
    state1 = (await client.get("/doctor/onboarding", headers=_bearer(token))).json()
    assert state1["retry_paused"] is True
    assert state1["config_reminder_paused"] is False

    r2 = await client.post(
        "/doctor/onboarding/pause", headers=_bearer(token), json={"config_reminders": True}
    )
    assert r2.status_code == 200, r2.text
    state2 = (await client.get("/doctor/onboarding", headers=_bearer(token))).json()
    # retries stays paused (omitted field leaves the existing switch untouched).
    assert state2["retry_paused"] is True
    assert state2["config_reminder_paused"] is True

    r3 = await client.post(
        "/doctor/onboarding/pause", headers=_bearer(token), json={"retries": False}
    )
    assert r3.status_code == 200, r3.text
    state3 = (await client.get("/doctor/onboarding", headers=_bearer(token))).json()
    assert state3["retry_paused"] is False
    assert state3["config_reminder_paused"] is True


# ===========================================================================================
# GET /doctor/professionals
# ===========================================================================================


async def test_list_professionals_joins_local_user_linkage(client, monkeypatch):
    async def fake_config_status(tenant_id):
        return {
            "professionals": [
                {
                    "id": str(OWNER_A_PROFESSIONAL_ID),
                    "name": "Owner A",
                    "is_active": True,
                    "has_calendar": True,
                    "has_hours": True,
                    "has_services": False,
                    "complete": False,
                },
                {
                    "id": str(uuid4()),
                    "name": "Unlinked Prof",
                    "is_active": True,
                    "has_calendar": False,
                    "has_hours": False,
                    "has_services": False,
                    "complete": False,
                },
            ]
        }

    monkeypatch.setattr(secretaria_provisioning, "get_config_status", fake_config_status)

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/doctor/professionals", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 2

    linked = next(p for p in items if p["id"] == str(OWNER_A_PROFESSIONAL_ID))
    assert linked["linked_user_email"] == OWNER_A_EMAIL
    assert linked["invite_pending"] is False  # the seeded owner has no invite token.

    unlinked = next(p for p in items if p["name"] == "Unlinked Prof")
    assert unlinked["linked_user_email"] is None
    assert unlinked["invite_pending"] is False


# ===========================================================================================
# POST /doctor/professionals/invites — end to end
# ===========================================================================================


async def test_invite_professional_end_to_end(client, monkeypatch):
    new_professional_id = str(uuid4())
    create_calls: list[dict] = []

    async def fake_create_professional(tenant_id, *, name, specialty, about):
        create_calls.append({"name": name, "specialty": specialty, "about": about})
        return {"professional_id": new_professional_id, "created": True}

    monkeypatch.setattr(secretaria_provisioning, "create_professional", fake_create_professional)
    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _noop_async(True))

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/professionals/invites",
        headers=_bearer(token),
        json={"name": "Dr. Invited", "email": "invited.doc@example.com", "specialty": "Cardio"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["professional_id"] == new_professional_id
    assert "/convite?token=" in body["invite_link"]
    assert create_calls == [{"name": "Dr. Invited", "specialty": "Cardio", "about": None}]

    raw_token = body["invite_link"].split("token=")[1]

    exchange = await client.post("/auth/exchange-invite-token", json={"token": raw_token})
    assert exchange.status_code == 200, exchange.text
    access_token = exchange.json()["access_token"]
    assert exchange.json()["professional_id"] == new_professional_id

    set_pw = await client.post(
        "/auth/set-password",
        headers=_bearer(access_token),
        json={"new_password": "invitedpass1"},
    )
    assert set_pw.status_code == 204, set_pw.text

    login = await client.post(
        "/auth/token", json={"email": "invited.doc@example.com", "password": "invitedpass1"}
    )
    assert login.status_code == 200, login.text

    # The token is single-use: re-exchanging it now fails.
    reuse = await client.post("/auth/exchange-invite-token", json={"token": raw_token})
    assert reuse.status_code == 401
    assert reuse.json()["detail"] == "invalid_invite_token"


async def test_invite_professional_email_failure_still_returns_invite_link(client, monkeypatch):
    monkeypatch.setattr(
        secretaria_provisioning,
        "create_professional",
        _noop_async({"professional_id": str(uuid4()), "created": True}),
    )
    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _noop_async(False))

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/professionals/invites",
        headers=_bearer(token),
        json={"name": "Dr. NoEmail", "email": "no.email.sent@example.com"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["invite_link"]


async def test_invite_professional_duplicate_email_conflict(client, monkeypatch):
    monkeypatch.setattr(
        secretaria_provisioning,
        "create_professional",
        _noop_async({"professional_id": str(uuid4()), "created": True}),
    )
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/professionals/invites",
        headers=_bearer(token),
        json={"name": "Dup", "email": OWNER_A_EMAIL},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "email_already_registered"


async def test_invite_professional_secretaria_failure_returns_502(client, monkeypatch):
    monkeypatch.setattr(secretaria_provisioning, "create_professional", _noop_async(None))
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/professionals/invites",
        headers=_bearer(token),
        json={"name": "Dr. Down", "email": "down@example.com"},
    )
    assert resp.status_code == 502


async def test_invite_professional_allows_staff(client, monkeypatch):
    """Corrections round (2026-07-22): invites are no longer owner-only — any doctor
    (owner or staff) may invite a professional now that the endpoint depends on
    `require_doctor` instead of `require_owner`."""
    monkeypatch.setattr(
        secretaria_provisioning,
        "create_professional",
        _noop_async({"professional_id": str(uuid4()), "created": True}),
    )
    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _noop_async(True))

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    staff_email = "invite.staff@a.com"
    await _create_staff(client, admin_token, tenant_a_id, staff_email)
    staff_token = await _token(client, staff_email, "staffpass1")

    resp = await client.post(
        "/doctor/professionals/invites",
        headers=_bearer(staff_token),
        json={"name": "Staff Invited Doc", "email": "staff.invited@example.com"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["invite_link"]


# ===========================================================================================
# POST /doctor/professionals/self
# ===========================================================================================


async def test_self_bind_success_for_unbound_owner(client, monkeypatch):
    new_id = str(uuid4())
    monkeypatch.setattr(
        secretaria_provisioning,
        "create_professional",
        _noop_async({"professional_id": new_id, "created": True}),
    )
    # Owner B starts with no professional_id (see tests/test_rbac.py fixture).
    token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    resp = await client.post(
        "/doctor/professionals/self", headers=_bearer(token), json={"specialty": "Geral"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"professional_id": new_id, "created": True}

    me = await client.get("/auth/me", headers=_bearer(token))
    assert me.json()["user"]["professional_id"] == new_id


async def test_self_bind_conflict_when_already_bound(client):
    # Owner A is seeded WITH a professional_id already (OWNER_A_PROFESSIONAL_ID).
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post("/doctor/professionals/self", headers=_bearer(token), json={})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "already_bound"


async def test_self_bind_allows_staff(client, monkeypatch):
    """Corrections round (2026-07-22): self-bind is no longer owner-only — any doctor
    (owner or staff) may bind themselves to a professional now."""
    new_id = str(uuid4())
    monkeypatch.setattr(
        secretaria_provisioning,
        "create_professional",
        _noop_async({"professional_id": new_id, "created": True}),
    )
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    staff_email = "self.staff@a.com"
    await _create_staff(client, admin_token, tenant_a_id, staff_email)
    staff_token = await _token(client, staff_email, "staffpass1")

    resp = await client.post("/doctor/professionals/self", headers=_bearer(staff_token), json={})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"professional_id": new_id, "created": True}


# ===========================================================================================
# Internal: GET /internal/onboarding/tenants + POST .../events
# ===========================================================================================


def _set_pair_key(monkeypatch, key: str = "pair-key") -> None:
    import brain_api.api.internal as internal_api

    # STRIPE_TRIAL_PERIOD_DAYS/FRONTEND_BASE_URL default to the same values as the real
    # Settings class (Task 2's test-window fields on GET /internal/onboarding/tenants,
    # services.onboarding_sync.test_window_email_due) -- present here so ANY test using
    # this helper can reach the listing endpoint too, not just the pair-key gate itself.
    fake_settings = SimpleNamespace(
        SECRETARIA_API_KEY=key,
        SECRETARIA_API_KEY_PREVIOUS="",
        STRIPE_TRIAL_PERIOD_DAYS=0,
        FRONTEND_BASE_URL="http://localhost:3000",
    )
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)


async def test_internal_onboarding_tenants_key_gate(client, monkeypatch):
    unauth = await client.get(
        "/internal/onboarding/tenants", headers={"X-Internal-Api-Key": "whatever"}
    )
    assert unauth.status_code == 403  # unset SECRETARIA_API_KEY (conftest) -> fail closed.

    _set_pair_key(monkeypatch)
    wrong = await client.get(
        "/internal/onboarding/tenants", headers={"X-Internal-Api-Key": "not-the-key"}
    )
    assert wrong.status_code == 401


async def test_internal_onboarding_tenants_lists_non_ativo_and_excludes_ativo(client, monkeypatch):
    _set_pair_key(monkeypatch)

    # Drive tenant A all the way to 'ativo'/'completa' so it must be EXCLUDED from the
    # list; tenant B is untouched and stays 'pending'/'incompleta' -> must be INCLUDED.
    monkeypatch.setattr(
        secretaria_provisioning,
        "connect_whatsapp",
        _noop_async(secretaria_provisioning.CONNECTION_OK),
    )
    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _noop_async(True))
    monkeypatch.setattr(
        secretaria_provisioning,
        "get_config_status",
        _noop_async({"connected": True, "config_complete": True, "mode_resolved": True}),
    )
    monkeypatch.setattr(
        secretaria_provisioning, "activate_tenant", _noop_async({"activated": True})
    )

    owner_a_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    pass_resp = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(owner_a_token),
        json={
            "attempt_id": str(uuid4()),
            "result": "pass",
            "phone_number_id": "5511999990000",
        },
    )
    assert pass_resp.status_code == 200, pass_resp.text
    assert pass_resp.json()["onboarding_state"] == "ativo"

    resp = await client.get(
        "/internal/onboarding/tenants", headers={"X-Internal-Api-Key": "pair-key"}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    clinic_names = {i["clinic_name"] for i in items}
    assert CLINIC_A not in clinic_names
    assert CLINIC_B in clinic_names

    row_b = next(i for i in items if i["clinic_name"] == CLINIC_B)
    assert row_b["onboarding_state"] == "pending"
    assert row_b["owner_email"] == OWNER_B_EMAIL
    assert row_b["owner_name"] == "Owner B"
    assert row_b["subscription_active"] is False  # tenant B has no entitlement row.


async def test_internal_onboarding_events_recurring_always_applies(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = (await _tenant_ids(client, admin_token))[CLINIC_B]

    first_retry = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    r1 = await client.post(
        f"/internal/onboarding/tenants/{tenant_b_id}/events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "event": "retry_nudge_sent",
            "at": datetime.now(UTC).isoformat(),
            "next_retry_at": first_retry,
        },
    )
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"applied": True}

    second_retry = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    r2 = await client.post(
        f"/internal/onboarding/tenants/{tenant_b_id}/events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "event": "retry_nudge_sent",
            "at": datetime.now(UTC).isoformat(),
            "next_retry_at": second_retry,
        },
    )
    assert r2.status_code == 200
    assert r2.json() == {"applied": True}  # recurring: always applies, overwrites.

    listing = (
        await client.get("/internal/onboarding/tenants", headers={"X-Internal-Api-Key": "pair-key"})
    ).json()["items"]
    row = next(i for i in listing if i["tenant_id"] == tenant_b_id)
    assert row["next_retry_at"].startswith(second_retry[:19])


async def test_internal_onboarding_events_one_shot_idempotent(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = (await _tenant_ids(client, admin_token))[CLINIC_B]

    first_at = datetime.now(UTC).isoformat()
    r1 = await client.post(
        f"/internal/onboarding/tenants/{tenant_b_id}/events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"event": "manual_review_flagged", "at": first_at},
    )
    assert r1.status_code == 200
    assert r1.json() == {"applied": True}

    second_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    r2 = await client.post(
        f"/internal/onboarding/tenants/{tenant_b_id}/events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"event": "manual_review_flagged", "at": second_at},
    )
    assert r2.status_code == 200
    assert r2.json() == {"applied": False}  # one-shot: already set -> no-op.

    listing = (
        await client.get("/internal/onboarding/tenants", headers={"X-Internal-Api-Key": "pair-key"})
    ).json()["items"]
    row = next(i for i in listing if i["tenant_id"] == tenant_b_id)
    # The ORIGINAL timestamp is preserved (not overwritten by the second, no-op call).
    assert row["manual_review_flagged_at"].startswith(first_at[:19])


async def test_internal_onboarding_events_unknown_tenant_404(client, monkeypatch):
    _set_pair_key(monkeypatch)
    resp = await client.post(
        f"/internal/onboarding/tenants/{uuid4()}/events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"event": "closing_email_sent", "at": datetime.now(UTC).isoformat()},
    )
    assert resp.status_code == 404
