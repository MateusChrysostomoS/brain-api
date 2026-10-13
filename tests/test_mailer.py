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


class _FakeSMTP:
    """Records what `_smtp_send` does on the wire, without a network."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout):
        self.starttls_context = None
        self.logged_in = False
        self.sent = False
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.starttls_context = context

    def login(self, user, password):
        self.logged_in = True

    def send_message(self, message):
        self.sent = True


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", _FakeSMTP)
    settings = get_settings()
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_USERNAME", "mailer-user")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "mailer-pass")
    monkeypatch.setattr(settings, "EMAIL_FROM_ADDRESS", "no-reply@example.com")
    return settings


async def test_starttls_verifies_the_server_certificate(fake_smtp):
    import ssl

    ok = await real_send_email(
        "ana@example.com", "patient_access_otp", CALLER_VARIABLES["patient_access_otp"]
    )
    assert ok is True
    (conn,) = _FakeSMTP.instances
    context = conn.starttls_context
    assert context is not None
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


async def test_credentials_are_never_sent_without_tls(fake_smtp, monkeypatch):
    monkeypatch.setattr(fake_smtp, "SMTP_USE_TLS", False)
    with capture_logs() as logs:
        ok = await real_send_email(
            "ana@example.com", "patient_access_otp", CALLER_VARIABLES["patient_access_otp"]
        )
    assert ok is False
    assert all(not conn.logged_in and not conn.sent for conn in _FakeSMTP.instances)
    assert any(e["event"] == "email_send_failed" for e in logs)
