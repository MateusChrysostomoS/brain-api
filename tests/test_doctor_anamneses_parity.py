"""brain-api -> PreCheck proxies added by TASK-011 (anamneses parity).

Unit tests of `services/precheck_client` first (method, path, forwarded bearer, body,
config gate), then the routes (Task 8). httpx is replaced by a recorder — no network.
The route tests reuse test_rbac's seeded app (Owner A: a PreCheck-entitled doctor).
"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from brain_api.services import precheck_client
from tests.test_rbac import OWNER_A_EMAIL, OWNER_A_PASSWORD, _bearer, _token

CONFIGURED = SimpleNamespace(
    PRECHECK_BASE_URL="http://precheck:8000",
    PRECHECK_TIMEOUT_SECONDS=10.0,
)
UNCONFIGURED = SimpleNamespace(PRECHECK_BASE_URL="", PRECHECK_TIMEOUT_SECONDS=10.0)
BEARER = "Bearer brain-jwt"


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
) -> list[dict]:
    """Configured PreCheck + an httpx client that records every request it gets."""
    calls: list[dict] = []

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def request(self, method, path, headers=None, params=None, json=None):
            calls.append(
                {
                    "method": method,
                    "path": path,
                    "headers": headers,
                    "params": params,
                    "json": json,
                    "base_url": self.kwargs.get("base_url"),
                }
            )
            if exc is not None:
                raise exc
            assert response is not None
            return response

    monkeypatch.setattr(precheck_client, "get_settings", lambda: CONFIGURED)
    monkeypatch.setattr(precheck_client.httpx, "AsyncClient", _FakeClient)
    return calls


def _forbid_httpx(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """PRECHECK_BASE_URL unset; building any httpx client is recorded (must stay empty)."""
    built: list[dict] = []

    def _no_client(**kwargs: object):
        built.append(dict(kwargs))
        raise AssertionError("PreCheck must not be called")

    monkeypatch.setattr(precheck_client, "get_settings", lambda: UNCONFIGURED)
    monkeypatch.setattr(precheck_client.httpx, "AsyncClient", _no_client)
    return built


# --- Task 7: the client ------------------------------------------------------------


async def test_client_media_list_is_a_stub_page_when_unconfigured(monkeypatch):
    built = _forbid_httpx(monkeypatch)
    assert await precheck_client.list_anamnesis_media(BEARER, 7) == {"items": [], "stub": True}
    assert built == []


@pytest.mark.parametrize(
    "call",
    [
        lambda: precheck_client.get_anamnesis_media_url(BEARER, 101),
        lambda: precheck_client.set_anamnesis_status(BEARER, 7, "approved"),
    ],
)
async def test_client_url_and_status_raise_503_when_unconfigured(monkeypatch, call):
    built = _forbid_httpx(monkeypatch)
    with pytest.raises(HTTPException) as err:
        await call()
    assert err.value.status_code == 503
    assert err.value.detail == "precheck_not_configured"
    assert built == []


async def test_client_forwards_method_path_bearer_and_body(monkeypatch):
    calls = _install_fake_httpx(monkeypatch, response=_FakeResponse(200, {"ok": True}))
    await precheck_client.list_anamnesis_media(BEARER, 7)
    await precheck_client.get_anamnesis_media_url(BEARER, 101)
    await precheck_client.set_anamnesis_status(BEARER, 7, "rejected")
    assert calls == [
        {
            "method": "GET",
            "path": "/api/v1/doctor/anamneses/7/media",
            "headers": {"Authorization": BEARER},
            "params": None,
            "json": None,
            "base_url": "http://precheck:8000",
        },
        {
            "method": "GET",
            "path": "/api/v1/doctor/anamneses/media/101/url",
            "headers": {"Authorization": BEARER},
            "params": None,
            "json": None,
            "base_url": "http://precheck:8000",
        },
        {
            "method": "PATCH",
            "path": "/api/v1/doctor/anamneses/7/status",
            "headers": {"Authorization": BEARER},
            "params": None,
            "json": {"status": "rejected"},
            "base_url": "http://precheck:8000",
        },
    ]


async def test_client_existing_list_still_sends_its_pagination(monkeypatch):
    calls = _install_fake_httpx(monkeypatch, response=_FakeResponse(200, {"items": []}))
    await precheck_client.list_anamneses(BEARER, 5, 10)
    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == "/api/v1/doctor/anamneses"
    assert calls[0]["params"] == {"skip": 5, "limit": 10}
