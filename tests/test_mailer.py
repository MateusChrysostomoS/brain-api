"""services/mailer.py — brain-api's own authentication e-mail sender (TASK-056)."""

import smtplib

import pytest
from structlog.testing import capture_logs

from brain_api.config import get_settings
from brain_api.services import mailer
from brain_api.services.mailer import render, send_email as real_send_email

# The variables each CALLER really passes (keep in sync with the call sites).
CALLER_VARIABLES = {
    "password_reset_code": {"name": "Ana", "code": "012345", "ttl_minutes": 10},
    "staff_temporary_password": {
        "name": "Ana",
        "clinic_name": "Clínica X",
        "email": "ana@example.com",
        "temporary_password": "Temp1234",
        "login_url": "https://portal.example.com/login/",
    },
    "professional_invite": {
        "name": "Ana",
        "clinic_name": "Clínica X",
        "link": "https://brain.example.com/convite?token=abc",
    },
    "patient_access_otp": {"code": "654321", "ttl_minutes": 10},
}


@pytest.fixture
def smtp_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "EMAIL_FROM_ADDRESS", "no-reply@example.com")
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(mailer, "_smtp_send", lambda to, s, b: sent.append((to, s, b)))
    return sent


def test_every_template_renders_with_its_caller_variables():
    assert set(CALLER_VARIABLES) == set(mailer.TEMPLATES)
    for template, variables in CALLER_VARIABLES.items():
        subject, body = render(template, variables)
        assert "{" not in subject and "{" not in body, template
        assert "Equipe Brain" in body


def test_code_keeps_its_leading_zero_in_the_body():
    _, body = render("password_reset_code", CALLER_VARIABLES["password_reset_code"])
    assert "012345" in body


async def test_unconfigured_smtp_returns_false_without_connecting(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("must not connect")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    with capture_logs() as logs:
        ok = await real_send_email(
            "ana@example.com", "patient_access_otp", CALLER_VARIABLES["patient_access_otp"]
        )
    assert ok is False
    assert any(entry["event"] == "email_not_configured" for entry in logs)


async def test_sends_and_logs_only_the_template(smtp_on):
    with capture_logs() as logs:
        ok = await real_send_email(
            "ana@example.com", "password_reset_code", CALLER_VARIABLES["password_reset_code"]
        )
    assert ok is True
    assert smtp_on[0][0] == "ana@example.com"
    flat = repr(logs)
    assert "ana@example.com" not in flat and "012345" not in flat
    assert any(entry["event"] == "email_sent" for entry in logs)


async def test_missing_variable_is_refused_not_sent(smtp_on):
    ok = await real_send_email("ana@example.com", "password_reset_code", {"name": "Ana"})
    assert ok is False
    assert smtp_on == []


async def test_unknown_template_is_refused(smtp_on):
    assert await real_send_email("ana@example.com", "nope", {}) is False
    assert smtp_on == []


async def test_header_injection_is_flattened(smtp_on):
    variables = dict(CALLER_VARIABLES["professional_invite"], clinic_name="X\r\nBcc: evil@x.com")
    assert await real_send_email(
        "ana@example.com\r\nBcc: e@x.com", "professional_invite", variables
    )
    to, subject, _ = smtp_on[0]
    assert "\n" not in subject and "\r" not in subject
    assert "\n" not in to and "\r" not in to


async def test_smtp_failure_returns_false(monkeypatch, smtp_on):
    def fail(*args):
        raise smtplib.SMTPException("down")

    monkeypatch.setattr(mailer, "_smtp_send", fail)
    with capture_logs() as logs:
        ok = await real_send_email(
            "ana@example.com", "patient_access_otp", CALLER_VARIABLES["patient_access_otp"]
        )
    assert ok is False
    assert any(
        e["event"] == "email_send_failed" and e["error_type"] == "SMTPException" for e in logs
    )
