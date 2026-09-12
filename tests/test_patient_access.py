"""Patient access by e-mail OTP + the product switchboard (Brain-Message, piece 7).

This is the piece that gives a patient WITHOUT WhatsApp an identity and a session, and
routes what they type to the right backend. What is worth pinning down, and the failure
mode each test exists to catch:

- The code is a credential. It must never be readable from the database or from a log
  line, and a wrong or stale one must be indistinguishable from one that never existed.
- Rate limits are two, not one: an attacker with many IPs flooding ONE inbox is invisible
  to a per-IP bucket.
- A thread list that drifts from `EntitlementOut` is how a patient gets a tab for a
  product the clinic never bought — or loses one it pays for. Both gates (`products` AND
  `channels.brain_message`) are load-bearing.
- The relay must present the RIGHT service secret under the RIGHT header name. secretarIA
  reads `X-Internal-Api-Key`; PreCheck reads `X-Internal-Token`. They are different
  secrets under different names, and swapping them authenticates nothing.
- Tenant isolation is structural here: the tenant is read off the session, never from
  input. The tests prove the structure holds even when a token is forged to disagree.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from brain_api.core.cookies import PATIENT_SESSION_COOKIE_NAME
from brain_api.core.database import Base, get_session
from brain_api.core.security import (
    PATIENT_TOKEN_SCOPE,
    create_patient_token,
    decode_token,
    hash_refresh_token,
)
from brain_api.main import app
from brain_api.models import Entitlement, Tenant
from brain_api.models.patient_access import (
    CONSENT_KIND_CHANNEL_ACCESS,
    MessagePatient,
    MessagePatientOtp,
    PatientConsentEvent,
)
from brain_api.services import patient_access

PATIENT_EMAIL = "paciente@exemplo.com"

# Three clinics, one distinguishing property each — every gate in this vertical is a
# conjunction, so a single "everything on" tenant would prove nothing about which half
# of it does the work.
CLINIC_BOTH = "Clínica Dois Produtos"  # brain_message ON, precheck + secretaria
CLINIC_ONLY_SECRETARIA = "Clínica Só secretarIA"  # brain_message ON, secretaria only
CLINIC_CHANNEL_OFF = "Clínica Sem Canal"  # brain_message OFF, both products


class _Seed:
    """The tenant ids the fixture created, by role in the tests."""

    def __init__(self, both: uuid.UUID, only_secretaria: uuid.UUID, channel_off: uuid.UUID):
        self.both = both
        self.only_secretaria = only_secretaria
        self.channel_off = channel_off


@pytest_asyncio.fixture
async def pclient():
    """`(client, sessionmaker, seed)` over a fresh in-memory DB with the three clinics."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with sessionmaker() as session, session.begin():
        ids = {}
        for name, channel, precheck, secretaria in (
            (CLINIC_BOTH, True, True, True),
            (CLINIC_ONLY_SECRETARIA, True, False, True),
            (CLINIC_CHANNEL_OFF, False, True, True),
        ):
            tenant = Tenant(clinic_name=name, brain_message_enabled=channel)
            session.add(tenant)
            await session.flush()
            session.add(
                Entitlement(
                    tenant_id=tenant.id,
                    precheck_enabled=precheck,
                    secretaria_enabled=secretaria,
                    plan="complete_clinic_combo",
                    status="active",
                )
            )
            ids[name] = tenant.id
        seed = _Seed(ids[CLINIC_BOTH], ids[CLINIC_ONLY_SECRETARIA], ids[CLINIC_CHANNEL_OFF])

    async def _override_get_session():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c, sessionmaker, seed
    app.dependency_overrides.clear()
    await engine.dispose()


# --- helpers ---------------------------------------------------------------------------


async def _peek_code(sessionmaker, tenant_id, email=PATIENT_EMAIL) -> str:
    """Brute-force the code out of its HASH — the test's stand-in for reading the e-mail.

    Deliberately NOT a back door into the service: the only reason this works is that a
    6-digit space is small, which is exactly the property `PATIENT_OTP_MAX_ATTEMPTS`
    exists to defend. Recovering it this way (rather than having `issue_otp` hand it back
    in a test hook) keeps the production path free of any "return the plaintext" branch.
    """
    async with sessionmaker() as session:
        row = await session.scalar(
            select(MessagePatientOtp).where(
                MessagePatientOtp.tenant_id == tenant_id,
                MessagePatientOtp.email == email,
            )
        )
    assert row is not None, "no challenge was issued"
    for candidate in range(10**6):
        code = f"{candidate:06d}"
        if hash_refresh_token(code) == row.code_hash:
            return code
    raise AssertionError("code not recoverable — did PATIENT_OTP_LENGTH change?")


async def _login(client, sessionmaker, tenant_id, email=PATIENT_EMAIL):
    """Full request -> verify round trip. Returns the `verify-otp` response."""
    resp = await client.post(
        "/patient-access/request-otp", json={"tenant_id": str(tenant_id), "email": email}
    )
    assert resp.status_code == 200, resp.text
    code = await _peek_code(sessionmaker, tenant_id, email)
    return await client.post(
        "/patient-access/verify-otp",
        json={"tenant_id": str(tenant_id), "email": email, "code": code},
    )


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _SpyResponse:
    """Just enough of `httpx.Response` for `message_switchboard._call`."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"status": "queued"}

    def json(self):
        return self._payload


def _spy_transport(monkeypatch, *, status_code=200, payload=None):
    """Replace `httpx.AsyncClient.request` and record every outbound hop.

    A spy rather than an assertion on the RESPONSE, because the thing under test is what
    brain-api SENT — the header name, the secret, the path and the body shape. A relay
    that returned the right thing while presenting the wrong header would pass a
    response-only test and fail in production against a service that is working.
    """
    calls: list[dict] = []
    real_request = httpx.AsyncClient.request

    async def _fake_request(self, method, url, *, headers=None, json=None, params=None, **kw):
        # The test client reaches the app through this same method. Intercept ONLY the
        # outbound mesh hops; anything else is delegated, or patching the spy in would
        # sever the test's own connection to the app under test.
        mesh = "secretaria:8000" in str(self.base_url) or "precheck:8000" in str(self.base_url)
        # The OTP e-mail rides the SAME secretarIA base URL
        # (`send_notification_email`). It is not a switchboard hop, so it must not
        # land in `calls` — otherwise every "no network happened" assertion below
        # would count the login's own e-mail and pass for the wrong reason.
        if not mesh or str(url).endswith("/internal/notifications/email"):
            return await real_request(
                self, method, url, headers=headers, json=json, params=params, **kw
            )
        calls.append(
            {
                "base_url": str(self.base_url),
                "method": method,
                "url": str(url),
                "headers": dict(headers or {}),
                "json": json,
                "params": params,
            }
        )
        return _SpyResponse(status_code=status_code, payload=payload)

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    return calls


def _configure_mesh(monkeypatch):
    """Point both upstreams somewhere and give each its OWN secret.

    Two DIFFERENT values on purpose: if the code ever sent secretarIA's key to PreCheck
    (or under the wrong header), a shared fixture value would hide it.
    """
    from brain_api.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "SECRETARIA_BASE_URL", "http://secretaria:8000")
    monkeypatch.setattr(settings, "SECRETARIA_API_KEY", "secretaria-key-AAA")
    monkeypatch.setattr(settings, "PRECHECK_BASE_URL", "http://precheck:8000")
    monkeypatch.setattr(settings, "PRECHECK_INTERNAL_TOKEN", "precheck-token-BBB")


# --- 1) The OTP round trip --------------------------------------------------------------


async def test_request_then_verify_issues_a_patient_session(pclient):
    """The happy path: a code by e-mail becomes a scoped session for exactly one clinic."""
    client, sessionmaker, seed = pclient
    resp = await _login(client, sessionmaker, seed.both)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["token_type"] == "bearer"
    assert body["tenant_id"] == str(seed.both)
    assert uuid.UUID(body["patient_ref"])  # a real UUID, the sibling services' handle

    # The revocable leg went out as the hardened cookie, NOT in the body.
    assert PATIENT_SESSION_COOKIE_NAME in resp.cookies
    assert "refresh" not in body and "session_token" not in body

    # The access leg is a PATIENT token: scoped, tenant-bearing, roleless.
    claims = decode_token(body["access_token"])
    assert claims["scope"] == PATIENT_TOKEN_SCOPE
    assert claims["tenant_id"] == str(seed.both)
    assert claims["sub"] == body["patient_ref"]
    assert "role" not in claims and "is_owner" not in claims


async def test_patient_token_cannot_open_a_staff_route(pclient):
    """The scope claim is the wall: a patient session is refused by `get_current_principal`.

    `GET /entitlements` is the cheapest staff route that reads a tenant, and it is exactly
    the one a patient token "almost" satisfies — it carries a tenant_id. It must still be
    401, not 200 with the clinic's plan and usage counters.
    """
    client, sessionmaker, seed = pclient
    body = (await _login(client, sessionmaker, seed.both)).json()
    resp = await client.get("/entitlements", headers=_bearer(body["access_token"]))
    assert resp.status_code == 401


async def test_wrong_code_is_rejected_and_costs_an_attempt(pclient):
    """A wrong guess is a 400 with the generic message, and it is COUNTED."""
    client, sessionmaker, seed = pclient
    await client.post(
        "/patient-access/request-otp",
        json={"tenant_id": str(seed.both), "email": PATIENT_EMAIL},
    )
    real = await _peek_code(sessionmaker, seed.both)
    wrong = "000000" if real != "000000" else "111111"

    resp = await client.post(
        "/patient-access/verify-otp",
        json={"tenant_id": str(seed.both), "email": PATIENT_EMAIL, "code": wrong},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Código inválido ou expirado"

    async with sessionmaker() as session:
        row = await session.scalar(
            select(MessagePatientOtp).where(MessagePatientOtp.tenant_id == seed.both)
        )
        assert row.attempts == 1, "a failed guess that is not counted is not rate-limited"
        # And no identity was minted for a failed verification.
        assert await session.scalar(select(func.count()).select_from(MessagePatient)) == 0


async def test_expired_code_is_rejected(pclient):
    """Past `expires_at` the code is dead even though it is the RIGHT code."""
    client, sessionmaker, seed = pclient
    await client.post(
        "/patient-access/request-otp",
        json={"tenant_id": str(seed.both), "email": PATIENT_EMAIL},
    )
    code = await _peek_code(sessionmaker, seed.both)
    async with sessionmaker() as session, session.begin():
        row = await session.scalar(
            select(MessagePatientOtp).where(MessagePatientOtp.tenant_id == seed.both)
        )
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    resp = await client.post(
        "/patient-access/verify-otp",
        json={"tenant_id": str(seed.both), "email": PATIENT_EMAIL, "code": code},
    )
    assert resp.status_code == 400


async def test_code_is_single_use(pclient):
    """The second presentation of a correct code fails — `consumed_at` is burned."""
    client, sessionmaker, seed = pclient
    await client.post(
        "/patient-access/request-otp",
        json={"tenant_id": str(seed.both), "email": PATIENT_EMAIL},
    )
    code = await _peek_code(sessionmaker, seed.both)
    payload = {"tenant_id": str(seed.both), "email": PATIENT_EMAIL, "code": code}
    assert (await client.post("/patient-access/verify-otp", json=payload)).status_code == 200
    assert (await client.post("/patient-access/verify-otp", json=payload)).status_code == 400


async def test_attempts_ceiling_burns_the_challenge(pclient):
    """After PATIENT_OTP_MAX_ATTEMPTS wrong guesses the RIGHT code stops working.

    This is the real defence for a 6-digit secret: without it, an attacker with a pool of
    IPs walks around the per-IP limiter and enumerates the space.
    """
    from brain_api.config import get_settings

    client, sessionmaker, seed = pclient
    await client.post(
        "/patient-access/request-otp",
        json={"tenant_id": str(seed.both), "email": PATIENT_EMAIL},
    )
    code = await _peek_code(sessionmaker, seed.both)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(get_settings().PATIENT_OTP_MAX_ATTEMPTS):
        await client.post(
            "/patient-access/verify-otp",
            json={"tenant_id": str(seed.both), "email": PATIENT_EMAIL, "code": wrong},
        )
    resp = await client.post(
        "/patient-access/verify-otp",
        json={"tenant_id": str(seed.both), "email": PATIENT_EMAIL, "code": code},
    )
    assert resp.status_code == 400


async def test_no_plaintext_code_reaches_the_logs(pclient, caplog):
    """GREP THE ACTUAL LOG OUTPUT — the code must not appear anywhere in it.

    Asserted against captured output rather than by reading the source, because the leak
    this guards against is never the log line someone wrote on purpose: it is a `%r` of a
    payload, an exception rendering a request body, or a structlog processor that widens
    later. Also checks the address, which is personal data on the same line.
    """
    client, sessionmaker, seed = pclient
    with caplog.at_level(logging.DEBUG):
        await client.post(
            "/patient-access/request-otp",
            json={"tenant_id": str(seed.both), "email": PATIENT_EMAIL},
        )
        code = await _peek_code(sessionmaker, seed.both)
        await client.post(
            "/patient-access/verify-otp",
            json={"tenant_id": str(seed.both), "email": PATIENT_EMAIL, "code": code},
        )

    # The CODE is checked against EVERY record, the database driver's own DEBUG echo
    # of bound parameters included — nothing anywhere may carry it in the clear, and
    # this is what proves the column really holds only a hash.
    everything = '\n'.join(r.getMessage() for r in caplog.records)
    assert code not in everything, "the OTP code leaked into a log line"

    # The ADDRESS is checked against THIS service's loggers only. aiosqlite echoes
    # every bound parameter at DEBUG, so the e-mail necessarily shows up in the
    # driver output of the INSERT that stores it — an artifact of SQLite echo in the
    # test harness, not something brain-api logged. Narrowing the scope keeps this
    # an assertion about our code instead of a permanently-red one about the driver.
    app_logs = '\n'.join(
        r.getMessage()
        for r in caplog.records
        if not r.name.startswith(("aiosqlite", "sqlalchemy"))
    )
    assert PATIENT_EMAIL not in app_logs, "the patient e-mail leaked into a log line"
    # And it is not in the database in the clear either.
    async with sessionmaker() as session:
        row = await session.scalar(select(MessagePatientOtp))
        assert row.code_hash != code
        assert row.code_hash == hash_refresh_token(code)


# --- 2) Enumeration + rate limits --------------------------------------------------------


async def test_request_otp_never_reveals_whether_the_clinic_is_reachable(pclient):
    """Same status AND same body for a real clinic, a channel-off one, and a made-up id."""
    client, sessionmaker, seed = pclient
    bodies = []
    for tenant_id in (seed.both, seed.channel_off, uuid.uuid4()):
        resp = await client.post(
            "/patient-access/request-otp",
            json={"tenant_id": str(tenant_id), "email": PATIENT_EMAIL},
        )
        assert resp.status_code == 200
        bodies.append(resp.json())
    assert bodies[0] == bodies[1] == bodies[2]

    # And a channel-off clinic issued no challenge at all — the answer was a pure façade.
    async with sessionmaker() as session:
        rows = (
            await session.scalars(
                select(MessagePatientOtp).where(
                    MessagePatientOtp.tenant_id == seed.channel_off
                )
            )
        ).all()
        assert rows == []


async def test_request_otp_rate_limit_trips_per_ip(pclient, monkeypatch):
    """The N+1st request from one IP is a 429.

    The limiter instance is monkeypatched directly rather than through the setting, the
    convention `tests/test_signup.py` already follows: `conftest` disables these buckets
    globally so the rest of the suite can log in freely.
    """
    from brain_api.api import patient_access as router_mod

    client, sessionmaker, seed = pclient
    # Module-level limiters keep their buckets between tests: start from an empty
    # window, or this test inherits hits from every login above it.
    router_mod._ip_limiter._hits.clear()
    router_mod._email_limiter._hits.clear()
    monkeypatch.setattr(router_mod._ip_limiter, "_limit_getter", lambda: 2)
    monkeypatch.setattr(router_mod._email_limiter, "_limit_getter", lambda: 999)

    body = {"tenant_id": str(seed.both), "email": PATIENT_EMAIL}
    assert (await client.post("/patient-access/request-otp", json=body)).status_code == 200
    assert (await client.post("/patient-access/request-otp", json=body)).status_code == 200
    assert (await client.post("/patient-access/request-otp", json=body)).status_code == 429


async def test_request_otp_rate_limit_trips_per_address(pclient, monkeypatch):
    """A DIFFERENT bucket: one inbox is protected even when every request has a new IP.

    The per-IP limiter is left wide open here on purpose — this is precisely the attack
    it cannot see, and the reason there are two buckets instead of one.
    """
    from brain_api.api import patient_access as router_mod

    client, sessionmaker, seed = pclient
    router_mod._ip_limiter._hits.clear()
    router_mod._email_limiter._hits.clear()
    monkeypatch.setattr(router_mod._ip_limiter, "_limit_getter", lambda: 999)
    monkeypatch.setattr(router_mod._email_limiter, "_limit_getter", lambda: 2)

    body = {"tenant_id": str(seed.both), "email": PATIENT_EMAIL}
    for i in range(2):
        resp = await client.post(
            "/patient-access/request-otp",
            json=body,
            headers={"X-Forwarded-For": f"203.0.113.{i}"},
        )
        assert resp.status_code == 200
    resp = await client.post(
        "/patient-access/request-otp", json=body, headers={"X-Forwarded-For": "203.0.113.9"}
    )
    assert resp.status_code == 429


# --- 3) Consent ---------------------------------------------------------------------------


async def test_consent_event_is_recorded_once_at_identity_creation(pclient):
    """One row on first verify, and STILL one after the patient logs in again.

    secretarIA's rule, mirrored: consent is recorded at patient creation, never per
    message and never per session — a per-login row would turn an audit trail into a
    login counter.
    """
    client, sessionmaker, seed = pclient
    first = (await _login(client, sessionmaker, seed.both)).json()
    async with sessionmaker() as session:
        rows = (await session.scalars(select(PatientConsentEvent))).all()
    assert len(rows) == 1
    assert rows[0].tenant_id == seed.both
    assert rows[0].subject_ref == first["patient_ref"]
    assert rows[0].kind == CONSENT_KIND_CHANNEL_ACCESS
    assert rows[0].legal_basis

    second = (await _login(client, sessionmaker, seed.both)).json()
    assert second["patient_ref"] == first["patient_ref"], "a re-login must not fork identity"
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(PatientConsentEvent)) == 1


# --- 4) Threads: exactly `products` x `channels.brain_message` ---------------------------


async def test_threads_list_both_products_when_the_clinic_has_both(pclient):
    client, sessionmaker, seed = pclient
    body = (await _login(client, sessionmaker, seed.both)).json()
    resp = await client.get("/patient-access/threads", headers=_bearer(body["access_token"]))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [t["product"] for t in data] == ["secretaria", "precheck"]
    assert {t["clinic_name"] for t in data} == {CLINIC_BOTH}


async def test_threads_omit_a_product_the_clinic_did_not_buy(pclient):
    """`products.precheck == false` means no PreCheck tab — not a disabled one."""
    client, sessionmaker, seed = pclient
    body = (await _login(client, sessionmaker, seed.only_secretaria)).json()
    resp = await client.get("/patient-access/threads", headers=_bearer(body["access_token"]))
    assert [t["product"] for t in resp.json()["data"]] == ["secretaria"]


async def test_channel_off_means_no_threads_even_with_a_valid_session(pclient):
    """The channel gate outranks the product gate.

    Proved with a session that was minted while the channel was ON and a clinic that owns
    BOTH products — so the empty list can only come from `channels.brain_message`, never
    from an entitlement. This is the drift the two-gate design exists to prevent.
    """
    client, sessionmaker, seed = pclient
    body = (await _login(client, sessionmaker, seed.both)).json()
    async with sessionmaker() as session, session.begin():
        tenant = await session.get(Tenant, seed.both)
        tenant.brain_message_enabled = False

    resp = await client.get("/patient-access/threads", headers=_bearer(body["access_token"]))
    assert resp.status_code == 200
    assert resp.json()["data"] == []


async def test_threads_requires_a_patient_session(pclient):
    client, sessionmaker, seed = pclient
    assert (await client.get("/patient-access/threads")).status_code == 401
    assert (
        await client.get("/patient-access/threads", headers=_bearer("not-a-token"))
    ).status_code == 401


# --- 5) The relay: right service, right header, right secret ------------------------------


async def test_send_to_secretaria_uses_its_key_under_its_own_header(pclient, monkeypatch):
    """A SPY on the outbound hop — the assertion is what brain-api SENT, not what it got."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, payload={"status": "queued"})

    body = (await _login(client, sessionmaker, seed.both)).json()
    resp = await client.post(
        "/patient-access/threads/secretaria/messages",
        headers=_bearer(body["access_token"]),
        json={"text": "Bom dia, gostaria de marcar uma consulta"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["product"] == "secretaria"
    assert resp.json()["payload"] == {"status": "queued"}

    assert len(calls) == 1
    call = calls[0]
    assert call["base_url"].startswith("http://secretaria:8000")
    assert call["method"] == "POST"
    assert call["url"] == "/internal/brain-message/inbound"
    assert call["headers"]["X-Internal-Api-Key"] == "secretaria-key-AAA"
    assert "X-Internal-Token" not in call["headers"]
    # secretarIA's field name, the tenant off the SESSION, the patient's minted handle.
    assert call["json"]["tenant_id"] == str(seed.both)
    assert call["json"]["external_id"] == body["patient_ref"]
    assert call["json"]["text"] == "Bom dia, gostaria de marcar uma consulta"


async def test_send_to_precheck_uses_its_own_token_and_field_name(pclient, monkeypatch):
    """PreCheck reads a DIFFERENT header and calls the handle `session_ref`.

    Also pins the body to exactly the keys its `extra="forbid"` model accepts: one stray
    field (`external_id`, `dedupe_id`) is a 422 from a service that is working perfectly.
    """
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, payload={"session_ref": "x", "status": "question"})

    body = (await _login(client, sessionmaker, seed.both)).json()
    resp = await client.post(
        "/patient-access/threads/precheck/messages",
        headers=_bearer(body["access_token"]),
        json={"text": "sim", "patient_name": "Ana"},
    )
    assert resp.status_code == 200, resp.text

    call = calls[0]
    assert call["base_url"].startswith("http://precheck:8000")
    assert call["url"] == "/internal/brain-message/inbound"
    assert call["headers"]["X-Internal-Token"] == "precheck-token-BBB"
    assert "X-Internal-Api-Key" not in call["headers"]
    assert set(call["json"]) == {"tenant_id", "session_ref", "text", "patient_name"}
    assert call["json"]["session_ref"] == body["patient_ref"]


TAP_ID = "prof|8faa12e1-0000-0000-0000-000000000001"


async def test_a_portal_tap_reaches_secretaria_with_its_id(pclient, monkeypatch):
    """A tap on a reply button / list row: the label as `text`, the id beside it.

    secretarIA is the product that knows the field (and validates it against the cards it
    offered); brain-api only carries it. Without the field the body is byte-for-byte what
    it was before the field existed - a client built against the older contract changes
    nothing on the wire.
    """
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, payload={"status": "queued"})
    body = (await _login(client, sessionmaker, seed.both)).json()

    resp = await client.post(
        "/patient-access/threads/secretaria/messages",
        headers=_bearer(body["access_token"]),
        json={"text": "Dra. Ana", "interactive_reply_id": TAP_ID},
    )
    assert resp.status_code == 200, resp.text
    assert calls[0]["json"] == {
        "tenant_id": str(seed.both),
        "external_id": body["patient_ref"],
        "text": "Dra. Ana",
        "interactive_reply_id": TAP_ID,
    }

    resp = await client.post(
        "/patient-access/threads/secretaria/messages",
        headers=_bearer(body["access_token"]),
        json={"text": "oi"},
    )
    assert resp.status_code == 200, resp.text
    assert "interactive_reply_id" not in calls[1]["json"]


async def test_a_portal_tap_never_reaches_precheck(pclient, monkeypatch):
    """PreCheck's inbound model is `extra="forbid"` and has no such field: the id is
    dropped on that leg, and the label still goes as `text` (what its flow accepts)."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, payload={"session_ref": "x", "status": "question"})
    body = (await _login(client, sessionmaker, seed.both)).json()

    resp = await client.post(
        "/patient-access/threads/precheck/messages",
        headers=_bearer(body["access_token"]),
        json={"text": "Sim", "interactive_reply_id": "buttons|0"},
    )
    assert resp.status_code == 200, resp.text
    assert set(calls[0]["json"]) == {"tenant_id", "session_ref", "text"}
    assert calls[0]["json"]["text"] == "Sim"


@pytest.mark.parametrize(
    "bad",
    [
        {"text": "oi", "interactive_reply_id": ""},
        {"text": "oi", "interactive_reply_id": "x" * 257},
        {"text": "oi", "interactive_reply_id": "prof|1", "tenant_id": "other"},
    ],
)
async def test_the_tap_id_is_bounded_and_the_body_stays_closed(pclient, monkeypatch, bad):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, payload={"status": "queued"})
    body = (await _login(client, sessionmaker, seed.both)).json()

    resp = await client.post(
        "/patient-access/threads/secretaria/messages",
        headers=_bearer(body["access_token"]),
        json=bad,
    )
    assert resp.status_code == 422, resp.text
    assert calls == []


async def test_poll_relays_to_each_products_own_read_route(pclient, monkeypatch):
    """secretarIA needs `tenant_id` as a query param; PreCheck scopes by the ref alone."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, payload={"data": []})

    body = (await _login(client, sessionmaker, seed.both)).json()
    ref = body["patient_ref"]
    headers = _bearer(body["access_token"])

    await client.get(
        "/patient-access/threads/secretaria/messages?since=2026-09-08T00:00:00Z",
        headers=headers,
    )
    await client.get("/patient-access/threads/precheck/messages", headers=headers)

    assert calls[0]["url"] == f"/internal/brain-message/conversations/{ref}/messages"
    assert calls[0]["params"]["tenant_id"] == str(seed.both)
    assert calls[0]["params"]["since"] == "2026-09-08T00:00:00Z"
    assert calls[1]["url"] == f"/internal/brain-message/sessions/{ref}/messages"


async def test_relay_to_an_unowned_product_is_refused_before_any_network_call(pclient, monkeypatch):
    """A clinic with `secretaria` only cannot be made to talk to PreCheck.

    The spy proves the stronger claim: not merely that the answer is 403, but that NO
    outbound request happened — so an unowned product cannot be probed for existence,
    latency or error shape.
    """
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch)

    body = (await _login(client, sessionmaker, seed.only_secretaria)).json()
    resp = await client.post(
        "/patient-access/threads/precheck/messages",
        headers=_bearer(body["access_token"]),
        json={"text": "oi"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "product_unavailable"
    assert calls == [], "a refused product must not reach the network"


async def test_relay_is_refused_when_the_channel_is_switched_off(pclient, monkeypatch):
    """Same 403 for a product the clinic OWNS once the channel is off — and no network."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch)

    body = (await _login(client, sessionmaker, seed.both)).json()
    async with sessionmaker() as session, session.begin():
        (await session.get(Tenant, seed.both)).brain_message_enabled = False

    resp = await client.post(
        "/patient-access/threads/secretaria/messages",
        headers=_bearer(body["access_token"]),
        json={"text": "oi"},
    )
    assert resp.status_code == 403
    assert calls == []


async def test_unknown_product_name_is_refused(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch)
    body = (await _login(client, sessionmaker, seed.both)).json()
    resp = await client.post(
        "/patient-access/threads/whatsapp/messages",
        headers=_bearer(body["access_token"]),
        json={"text": "oi"},
    )
    assert resp.status_code == 403
    assert calls == []


async def test_unconfigured_upstream_is_503_not_502(pclient, monkeypatch):
    """No base URL / no key is an OPERATOR fact, and must not read as a sibling outage."""
    client, sessionmaker, seed = pclient
    calls = _spy_transport(monkeypatch)
    body = (await _login(client, sessionmaker, seed.both)).json()
    resp = await client.post(
        "/patient-access/threads/secretaria/messages",
        headers=_bearer(body["access_token"]),
        json={"text": "oi"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "product_channel_unconfigured"
    assert calls == []


async def test_upstream_error_body_never_reaches_the_patient(pclient, monkeypatch):
    """A 401 from a key MISMATCH becomes a generic 502 — never the patient's own 401."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    _spy_transport(monkeypatch, status_code=401, payload={"detail": "Token interno invalido"})

    body = (await _login(client, sessionmaker, seed.both)).json()
    resp = await client.post(
        "/patient-access/threads/precheck/messages",
        headers=_bearer(body["access_token"]),
        json={"text": "oi"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "product_error"
    assert "interno" not in resp.text


# --- 6) Tenant isolation ------------------------------------------------------------------


async def test_the_same_address_at_two_clinics_is_two_separate_patients(pclient, monkeypatch):
    """One human, two clinics, two handles — and each session relays only to its own.

    This is the isolation proof that matters for the relay: there is no request field a
    patient could aim elsewhere, so the outbound `tenant_id` can only be their own.
    """
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch)

    a = (await _login(client, sessionmaker, seed.both)).json()
    b = (await _login(client, sessionmaker, seed.only_secretaria)).json()
    assert a["patient_ref"] != b["patient_ref"]

    for body in (a, b):
        await client.post(
            "/patient-access/threads/secretaria/messages",
            headers=_bearer(body["access_token"]),
            json={"text": "oi"},
        )
    assert calls[0]["json"]["tenant_id"] == str(seed.both)
    assert calls[1]["json"]["tenant_id"] == str(seed.only_secretaria)
    assert calls[0]["json"]["external_id"] != calls[1]["json"]["external_id"]


async def test_a_token_pairing_one_patient_with_another_tenant_is_refused(pclient):
    """A FORGED-but-signed token whose tenant disagrees with the row is 401.

    Signed by this service, so it is not a signature test: it is the check that the tenant
    is re-read from the identity row instead of trusted from the claim. Without it, anyone
    who could mint a token (or replay a stale one after a tenant change) would follow a
    patient into a clinic that is not theirs.
    """
    client, sessionmaker, seed = pclient
    a = (await _login(client, sessionmaker, seed.both)).json()
    forged = create_patient_token(
        tenant_id=str(seed.only_secretaria), patient_ref=a["patient_ref"]
    )
    assert (
        await client.get("/patient-access/threads", headers=_bearer(forged))
    ).status_code == 401


async def test_logout_revokes_the_session_row_and_clears_the_cookie(pclient):
    """Logout is idempotent, always 200, and actually kills the server-side row."""
    client, sessionmaker, seed = pclient
    resp = await _login(client, sessionmaker, seed.both)
    raw = resp.cookies[PATIENT_SESSION_COOKIE_NAME]

    out = await client.post("/patient-access/logout")
    assert out.status_code == 200
    async with sessionmaker() as session:
        assert await patient_access.find_patient_session(session, raw) is None
    # Idempotent: a second logout with nothing to revoke is still a clean 200.
    assert (await client.post("/patient-access/logout")).status_code == 200
