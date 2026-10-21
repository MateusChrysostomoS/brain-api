"""Professional capacity refusals must survive the provisioning client."""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from brain_api.services import secretaria_provisioning
from tests.test_onboarding_endpoints import _FakeResponse, _install_fake_provisioning_httpx


@pytest.mark.parametrize(
    "status,code",
    [(403, "multi_professional_not_entitled"), (409, "professional_limit_reached")],
)
async def test_interactive_professional_creation_preserves_plan_refusal(monkeypatch, status, code):
    _install_fake_provisioning_httpx(monkeypatch, response=_FakeResponse(status, {"detail": code}))
    with pytest.raises(HTTPException) as error:
        await secretaria_provisioning.create_professional(
            uuid4(), name="Test Professional", specialty=None, about=None, interactive=True
        )
    assert (error.value.status_code, error.value.detail) == (status, code)


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
async def test_unknown_refusals_still_fail_closed(monkeypatch, status):
    _install_fake_provisioning_httpx(
        monkeypatch, response=_FakeResponse(status, {"detail": "unrecognized"})
    )
    assert (
        await secretaria_provisioning.create_professional(
            uuid4(), name="Test Professional", specialty=None, about=None, interactive=True
        )
        is None
    )


@pytest.mark.parametrize(
    "body", [None, [], {"detail": {"code": "multi_professional_not_entitled"}}]
)
async def test_malformed_forbidden_body_fails_closed(monkeypatch, body):
    _install_fake_provisioning_httpx(monkeypatch, response=_FakeResponse(403, body))
    assert (
        await secretaria_provisioning.create_professional(
            uuid4(), name="Test Professional", specialty=None, about=None, interactive=True
        )
        is None
    )


async def test_html_forbidden_body_fails_closed(monkeypatch):
    response = _FakeResponse(403, {})

    def invalid_json():
        raise ValueError("HTML response")

    monkeypatch.setattr(response, "json", invalid_json)
    _install_fake_provisioning_httpx(monkeypatch, response=response)
    assert (
        await secretaria_provisioning.create_professional(
            uuid4(), name="Test Professional", specialty=None, about=None, interactive=True
        )
        is None
    )


async def test_bootstrap_does_not_propagate_interactive_plan_refusal(monkeypatch):
    _install_fake_provisioning_httpx(
        monkeypatch, response=_FakeResponse(403, {"detail": "multi_professional_not_entitled"})
    )
    assert (
        await secretaria_provisioning.create_professional(
            uuid4(), name="Test Professional", specialty=None, about=None
        )
        is None
    )
