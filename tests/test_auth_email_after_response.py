"""Authentication e-mail on PUBLIC routes leaves AFTER the response (TASK-056).

brain-api sends this mail itself over SMTP (services/mailer.py) — seconds, not the
milliseconds of the old hop to secretarIA. Sent inside the request, the delay would mark
the branch that mails (a registered staff address; a clinic with the channel open) and
hold request threads on public endpoints. So these routes hand the send to
BackgroundTasks: the ASGI response is complete before the mailer runs.
"""

from httpx import ASGITransport, AsyncClient

from brain_api.main import app
from brain_api.services import mailer
from tests.test_patient_access import PATIENT_EMAIL, _bearer
from tests.test_patient_pending_session import _claim, _open
from tests.test_rbac import OWNER_A_EMAIL


class _OrderRecorder:
    """ASGI wrapper noting when the response body is fully sent."""

    def __init__(self, inner, events: list[str]) -> None:
        self.inner, self.events = inner, events

    async def __call__(self, scope, receive, send):
        async def _send(message):
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                self.events.append("response_done")

        await self.inner(scope, receive, _send)


def _record_mail(monkeypatch, events: list[str]) -> None:
    async def send(to, template, variables):
        events.append(f"mail:{template}")
        return True

    monkeypatch.setattr(mailer, "send_email", send)


def _client(events: list[str]) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_OrderRecorder(app, events)), base_url="https://test")


async def test_staff_reset_code_is_mailed_after_the_response(client, monkeypatch):
    events: list[str] = []
    _record_mail(monkeypatch, events)
    async with _client(events) as c:
        r = await c.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    assert r.status_code == 200
    assert events == ["response_done", "mail:password_reset_code"]


async def test_patient_login_code_is_mailed_after_the_response(pclient, monkeypatch):
    events: list[str] = []
    _record_mail(monkeypatch, events)
    async with _client(events) as c:
        r = await c.post("/patient-access/request-otp", json={"email": PATIENT_EMAIL})
    assert r.status_code == 200
    assert events == ["response_done", "mail:patient_access_otp"]


async def test_visit_code_is_mailed_after_the_response(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    assert (
        await _claim(client, monkeypatch, seed.both, opened["patient_ref"])
    ).status_code == 200
    events: list[str] = []
    _record_mail(monkeypatch, events)
    async with _client(events) as c:
        r = await c.post(
            "/patient-access/pending/request-otp", headers=_bearer(opened["pending_token"])
        )
    assert r.status_code == 200, r.text
    assert events == ["response_done", "mail:patient_access_otp"]
