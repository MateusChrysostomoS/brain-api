"""brain-api -> secretaria internal data path (/doctor/appointments + /doctor/patients).

Reuses the seeded in-memory app + doctor tokens from test_rbac's `client` fixture (imported
here so pytest builds it). The httpx layer in `services/secretaria_internal` is monkeypatched,
so these exercise the REAL client logic — config gate, header, error mapping — with no network:

  * unconfigured mesh  -> empty stub page (no upstream call, no 500)
  * configured + 200   -> secretaria's payload verbatim, scoped to the caller's tenant_id,
                          with the X-Internal-Api-Key header attached
  * configured + 401   -> brain-api surfaces 502, secretaria's body never leaks
  * network error      -> brain-api surfaces 502
"""

from types import SimpleNamespace

import httpx
import pytest

from brain_api.services import secretaria_internal

# Helpers/constants from the RBAC suite; the `client` fixture itself is provided via
# conftest re-export and injected by name (no import needed — avoids an F811 shadow).
from tests.test_rbac import OWNER_A_EMAIL, OWNER_A_PASSWORD, _bearer, _token

CONFIGURED = SimpleNamespace(
    SECRETARIA_BASE_URL="http://secretaria:8000",
    INTERNAL_API_KEY="match-key",
    SECRETARIA_TIMEOUT_SECONDS=10.0,
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
    """Point the client at a configured mesh and a fake httpx that records the call."""
    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def get(self, path: str, headers=None, params=None) -> _FakeResponse:
            captured["path"] = path
            captured["headers"] = headers
            captured["params"] = params
            if exc is not None:
                raise exc
            assert response is not None
            return response

    monkeypatch.setattr(secretaria_internal, "get_settings", lambda: CONFIGURED)
    monkeypatch.setattr(secretaria_internal.httpx, "AsyncClient", _FakeClient)
    return captured


async def _tenant_a_id(client) -> str:
    """The acting tenant id, read from the caller's own /doctor/me (server-resolved)."""
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    me = (await client.get("/doctor/me", headers=_bearer(token))).json()
    return me["tenant"]["id"]


# --------------------------------------------------------------------------
# Fail closed when the mesh is unconfigured (default test env: both unset)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["/doctor/appointments", "/doctor/patients"])
async def test_empty_page_when_secretaria_unconfigured(client, route: str) -> None:
    """No SECRETARIA_BASE_URL / INTERNAL_API_KEY => empty stub page, never a 500."""
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get(route, headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"data": [], "stub": True}


# --------------------------------------------------------------------------
# Configured path: passthrough, tenant scoping, header
# --------------------------------------------------------------------------


async def test_appointments_proxied_scoped_and_keyed(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"data": [{"id": "a1", "status": "scheduled"}]}
    captured = _install_fake_httpx(monkeypatch, response=_FakeResponse(200, payload))

    tenant_a = await _tenant_a_id(client)
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get(
        "/doctor/appointments?skip=5&limit=10", headers=_bearer(token)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == payload  # secretaria's body returned verbatim
    # Scoped to the caller's OWN tenant (path id == principal.tenant_id), never client input.
    assert captured["path"] == f"/internal/tenants/{tenant_a}/appointments"
    # The shared key is attached; skip->offset is translated.
    assert captured["headers"]["X-Internal-Api-Key"] == "match-key"
    assert captured["params"] == {"limit": 10, "offset": 5}
    assert captured["client_kwargs"]["base_url"] == "http://secretaria:8000"


async def test_patients_proxied_to_correct_path(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_fake_httpx(monkeypatch, response=_FakeResponse(200, {"data": []}))
    tenant_a = await _tenant_a_id(client)
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/doctor/patients", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert captured["path"] == f"/internal/tenants/{tenant_a}/patients"


# --------------------------------------------------------------------------
# Upstream failures collapse to 502 with no leak
# --------------------------------------------------------------------------


async def test_key_mismatch_surfaces_502_no_leak(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """secretaria's 401 (key mismatch) becomes a clean 502 — body never leaks to the doctor."""
    leaky = "Invalid internal API key."
    _install_fake_httpx(monkeypatch, response=_FakeResponse(401, {"detail": leaky}))
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/doctor/appointments", headers=_bearer(token))
    assert resp.status_code == 502
    assert leaky not in resp.text


async def test_secretaria_key_unset_403_degrades_to_empty_page(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """secretaria's 403 (its OWN key unset) is an unconfigured mesh => empty page, not 502.

    Per acceptance criterion #1: an unset key on EITHER side yields a safe empty page (no 500).
    secretaria emits 403 only for the server-unconfigured case (401 is the mismatch case).
    """
    captured = _install_fake_httpx(
        monkeypatch, response=_FakeResponse(403, {"detail": "Internal API not configured."})
    )
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/doctor/appointments", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"data": [], "stub": True}
    # It really did attempt the upstream call (not a local short-circuit) before degrading.
    assert "appointments" in captured["path"]


async def test_upstream_network_error_surfaces_502(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_httpx(monkeypatch, exc=httpx.ConnectError("boom"))
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/doctor/patients", headers=_bearer(token))
    assert resp.status_code == 502
