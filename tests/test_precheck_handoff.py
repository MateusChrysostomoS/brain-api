"""Tests for `POST /internal/precheck-handoff` — the secretarIA -> brain-api -> PreCheck
patient handoff (CONTRACTS.md §12.3, one leg of 3). brain-api is the entitlement
AUTHORITY here (PreCheck is never asked to re-check it) and keeps NO new state: pure
pass-through orchestration.

Ground truth: api/internal.py, services/precheck_handoff.py, schemas/internal.py.
Mirrors the pair-key gate pattern from test_usage_events.py and the fake-httpx
upstream-mapping pattern from test_doctor_secretaria.py / test_privacy.py.
"""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

import brain_api.api.internal as internal_api
from brain_api.services import precheck_handoff as precheck_handoff_service
from tests.test_rbac import ADMIN_EMAIL, ADMIN_PASSWORD, CLINIC_A, CLINIC_B, _bearer, _token

ROUTE = "/internal/precheck-handoff"
PHONE = "5511988887777"


async def _tenant_ids(client, admin_token: str) -> dict[str, str]:
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()
    return {t["clinic_name"]: t["id"] for t in tenants["items"]}


def _set_pair_key(monkeypatch, key: str = "pair-key") -> None:
    fake_settings = SimpleNamespace(SECRETARIA_API_KEY=key, SECRETARIA_API_KEY_PREVIOUS="")
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)


async def _patch_entitlement(client, admin_token: str, tenant_id: str, **fields) -> None:
    resp = await client.patch(
        f"/admin/tenants/{tenant_id}/entitlements", headers=_bearer(admin_token), json=fields
    )
    assert resp.status_code == 200, resp.text


CONFIGURED = SimpleNamespace(
    PRECHECK_BASE_URL="http://precheck:8000",
    PRECHECK_INTERNAL_TOKEN="precheck-internal-token",
    PRECHECK_TIMEOUT_SECONDS=10.0,
)


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


def _install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: _FakeResponse | None = None,
    exc: Exception | None = None,
) -> dict:
    """Point the outbound leg at a configured mesh + a fake httpx that records the call."""
    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def post(self, path: str, headers=None, json=None) -> _FakeResponse:
            captured["path"] = path
            captured["headers"] = headers
            captured["json"] = json
            if exc is not None:
                raise exc
            assert response is not None
            return response

    monkeypatch.setattr(precheck_handoff_service, "get_settings", lambda: CONFIGURED)
    monkeypatch.setattr(precheck_handoff_service.httpx, "AsyncClient", _FakeClient)
    return captured


def _body(tenant_id: str, phone_number: str = PHONE) -> dict:
    return {"tenant_id": tenant_id, "phone_number": phone_number}


# --- Pair-key gate ---------------------------------------------------------------


async def test_handoff_key_unset_forbidden(client):
    """SECRETARIA_API_KEY is forced empty by conftest -> fail closed with 403."""
    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "whatever"}, json=_body(str(uuid4()))
    )
    assert resp.status_code == 403


async def test_handoff_wrong_key_unauthorized(client, monkeypatch):
    _set_pair_key(monkeypatch)
    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "not-the-key"}, json=_body(str(uuid4()))
    )
    assert resp.status_code == 401


async def test_handoff_no_key_unauthorized(client, monkeypatch):
    _set_pair_key(monkeypatch)
    resp = await client.post(ROUTE, json=_body(str(uuid4())))
    assert resp.status_code == 401


# --- Entitlement gate: 403 not entitled (before any upstream call) -----------------


async def test_handoff_no_entitlement_row_403(client, monkeypatch):
    """Clinic B has no entitlement row -> default resolution (inactive, precheck off)."""
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = (await _tenant_ids(client, admin_token))[CLINIC_B]

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_b_id)
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "precheck_not_entitled"


async def test_handoff_inactive_status_403(client, monkeypatch):
    """Clinic A is precheck_enabled=True but status flipped to canceled -> refused."""
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    await _patch_entitlement(client, admin_token, tenant_a_id, status="canceled")

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "precheck_not_entitled"


async def test_handoff_precheck_disabled_403(client, monkeypatch):
    """Clinic A stays active/trialing but precheck_enabled is turned off -> refused."""
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    await _patch_entitlement(client, admin_token, tenant_a_id, precheck_enabled=False)

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "precheck_not_entitled"


# --- Unconfigured mesh: must fail LOUD (503), never degrade -------------------------


async def test_handoff_unconfigured_mesh_503(client, monkeypatch):
    """Entitled tenant, but PRECHECK_BASE_URL/PRECHECK_INTERNAL_TOKEN are unset in the
    hermetic test env (conftest) -> 503, no upstream call attempted."""
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "precheck_handoff_not_configured"


# --- Success mapping ----------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["seeded", "already_active"])
async def test_handoff_success_mapping(client, monkeypatch, outcome: str):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    captured = _install_fake_httpx(
        monkeypatch, response=_FakeResponse(200, {"status": outcome})
    )

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": outcome}
    assert captured["path"] == "/internal/precheck-handoff"
    assert captured["headers"]["X-Internal-Token"] == "precheck-internal-token"
    assert captured["json"] == {"brain_tenant_id": tenant_a_id, "phone_number": PHONE}
    assert captured["client_kwargs"]["base_url"] == "http://precheck:8000"


# --- Upstream 404 / 409 mapping ------------------------------------------------------


async def test_handoff_upstream_404_no_clinic(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    _install_fake_httpx(monkeypatch, response=_FakeResponse(404, {"detail": "not found"}))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "no_clinic_for_tenant"


async def test_handoff_upstream_409_conflict(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    _install_fake_httpx(monkeypatch, response=_FakeResponse(409, {"detail": "conflict"}))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "conflicting_active_session"


async def test_handoff_upstream_503_passthrough(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    _install_fake_httpx(monkeypatch, response=_FakeResponse(503, {"detail": "degraded"}))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] != "degraded"  # generic detail, upstream body not surfaced


# --- Upstream 5xx / 422 / network error collapse to 502, no leak --------------------


async def test_handoff_upstream_500_surfaces_502_no_leak(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    leaky = "some internal precheck stack trace"
    _install_fake_httpx(monkeypatch, response=_FakeResponse(500, {"detail": leaky}))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 502, resp.text
    assert leaky not in resp.text


async def test_handoff_upstream_422_surfaces_502_no_leak(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    leaky = "phone_number: field required"
    _install_fake_httpx(monkeypatch, response=_FakeResponse(422, {"detail": leaky}))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 502, resp.text
    assert leaky not in resp.text


async def test_handoff_network_error_surfaces_502(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    _install_fake_httpx(monkeypatch, exc=httpx.ConnectError("boom"))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 502, resp.text


# --- Validation ----------------------------------------------------------------------


@pytest.mark.parametrize("phone", ["", "1234567", "not-digits!", "1" * 16, "12 34 56 78"])
async def test_handoff_bad_phone_422(client, monkeypatch, phone: str):
    _set_pair_key(monkeypatch)
    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(str(uuid4()), phone)
    )
    assert resp.status_code == 422, resp.text


async def test_handoff_bad_tenant_id_422(client, monkeypatch):
    _set_pair_key(monkeypatch)
    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"tenant_id": "not-a-uuid", "phone_number": PHONE},
    )
    assert resp.status_code == 422, resp.text
