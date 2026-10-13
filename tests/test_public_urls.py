"""Public links share one configured base per frontend."""

import pytest
from fastapi import HTTPException

from brain_api.config import Settings
from brain_api.core.invite_codes import invite_link
from brain_api.services import team


@pytest.mark.parametrize("suffix", ["", "/", "///"])
def test_payment_returns_share_frontend_base(suffix):
    settings = Settings(_env_file=None, FRONTEND_BASE_URL=f" https://brain.example{suffix} ")
    success = "https://brain.example/checkout/sucesso/?session_id={CHECKOUT_SESSION_ID}"
    assert settings.STRIPE_CHECKOUT_SUCCESS_URL == "https://brain.example/app?checkout=success"
    assert settings.STRIPE_SIGNUP_CHECKOUT_SUCCESS_URL == success
    assert settings.STRIPE_CHECKOUT_CANCEL_URL == "https://brain.example/app?checkout=cancelled"
    assert settings.STRIPE_SIGNUP_CHECKOUT_CANCEL_URL == "https://brain.example/checkout/cancelado/"
    assert settings.STRIPE_PORTAL_RETURN_URL == "https://brain.example/app"


@pytest.mark.parametrize("suffix", ["", "/", "///"])
def test_staff_and_patient_invites_share_public_base(monkeypatch, suffix):
    settings = Settings(
        _env_file=None, BRAIN_MESSAGE_PUBLIC_URL=f" https://portal.example{suffix} "
    )
    monkeypatch.setattr(team, "get_settings", lambda: settings)
    monkeypatch.setattr("brain_api.core.invite_codes.get_settings", lambda: settings)
    assert team.staff_login_url() == "https://portal.example/login"
    assert invite_link("clinic-code") == "https://portal.example/clinicas/?convite=clinic-code"


@pytest.mark.parametrize(
    "base",
    [
        "",
        "https://portal.example/login",
        "https://portal.example/conversa",
        "https://portal.example?q=1",
        "https://portal.example#x",
        "https://user:pass@portal.example",
        "http://portal.example",
        "javascript:alert(1)",
    ],
)
def test_invalid_staff_base_fails_closed(monkeypatch, base):
    settings = Settings(_env_file=None, APP_ENV="production", BRAIN_MESSAGE_PUBLIC_URL=base)
    monkeypatch.setattr(team, "get_settings", lambda: settings)
    with pytest.raises(HTTPException) as exc:
        team.staff_login_url()
    assert exc.value.status_code == 503
