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
- The account model (2026-09-15; its own tests live in tests/test_patient_account_invites.py):
  the code opens an account and a clinic joins only by invite. What stays here is the
  transition contract the portal of 2026-09-14 still speaks (old bodies,
  `sibling_candidates`, the reopen-only confirm), a logout with a bearer that ends every
  session of the ADDRESS (never of the clinic), and access tokens that die with the session
  row their `sid` names.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from brain_api.core.cookies import (
    CLIENT_HEADER_NAME,
    CLIENT_HEADER_VALUE,
    PATIENT_SESSION_COOKIE_NAME,
)
from brain_api.core.database import Base, get_session
from brain_api.core.security import (
    ALGORITHM,
    PATIENT_TOKEN_SCOPE,
    create_patient_token,
    decode_token,
    hash_refresh_token,
)
from brain_api.main import app
from brain_api.models import Entitlement, Tenant
from brain_api.models.patient_access import (
    CONSENT_KIND_ACCOUNT_LINK,
    CONSENT_KIND_CHANNEL_ACCESS,
    MessagePatient,
    MessagePatientAccountOtp,
    MessagePatientSession,
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
        # The challenge is the ACCOUNT's, keyed by address; `tenant_id` stays in the
        # signature only because callers name the clinic they log in through.
        row = await session.scalar(
            select(MessagePatientAccountOtp).where(MessagePatientAccountOtp.email == email)
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
    # ...and bound to the live row the cookie names too, so a logout can end it. A login's
    # token descends from no other login: its `login_sid` is its own `sid`.
    assert claims["login_sid"] == claims["sid"]
    async with sessionmaker() as session:
        row = await session.get(MessagePatientSession, uuid.UUID(claims["sid"]))
    assert row is not None and row.revoked_at is None
    assert row.token_hash == hash_refresh_token(resp.cookies[PATIENT_SESSION_COOKIE_NAME])
    # A patient of ONE clinic: the old body plus the clinic's name and an empty list.
    assert body["clinic_name"] == CLINIC_BOTH
    assert body["sibling_candidates"] == []


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
            select(MessagePatientAccountOtp).where(MessagePatientAccountOtp.email == PATIENT_EMAIL)
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
            select(MessagePatientAccountOtp).where(MessagePatientAccountOtp.email == PATIENT_EMAIL)
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
        row = await session.scalar(select(MessagePatientAccountOtp))
        assert row.code_hash != code
        assert row.code_hash == hash_refresh_token(code)


# --- 2) Enumeration + rate limits --------------------------------------------------------


async def test_request_otp_never_reveals_whether_the_clinic_is_reachable(pclient):
    """Same status AND same body for a real clinic, a channel-off one, and a made-up id."""
    client, sessionmaker, seed = pclient
    bodies = []
    for tenant_id in (seed.channel_off, uuid.uuid4()):
        resp = await client.post(
            "/patient-access/request-otp",
            json={"tenant_id": str(tenant_id), "email": PATIENT_EMAIL},
        )
        assert resp.status_code == 200
        bodies.append(resp.json())
    # The old body naming a channel-off or unknown clinic issued no challenge at all.
    async with sessionmaker() as session:
        assert (await session.scalars(select(MessagePatientAccountOtp))).all() == []
    for body in ({"tenant_id": str(seed.both), "email": PATIENT_EMAIL}, {"email": PATIENT_EMAIL}):
        resp = await client.post("/patient-access/request-otp", json=body)
        assert resp.status_code == 200
        bodies.append(resp.json())
    assert bodies[0] == bodies[1] == bodies[2] == bodies[3]


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
    # Everything real except the tenant — the patient's own live session id included — so
    # the refusal can only come from the tenant disagreeing with the rows.
    real_sid = decode_token(a["access_token"])["sid"]
    forged = create_patient_token(
        tenant_id=str(seed.only_secretaria),
        patient_ref=a["patient_ref"],
        session_id=real_sid,
        login_session_id=real_sid,
    )
    assert (
        await client.get("/patient-access/threads", headers=_bearer(forged))
    ).status_code == 401


async def test_logout_revokes_the_session_row_and_clears_the_cookie(pclient):
    """Logout is idempotent, always 200, and actually kills the server-side row."""
    client, sessionmaker, seed = pclient
    resp = await _login(client, sessionmaker, seed.both)
    raw = resp.cookies[PATIENT_SESSION_COOKIE_NAME]

    token = resp.json()["access_token"]
    assert (
        await client.get("/patient-access/threads", headers=_bearer(token))
    ).status_code == 200

    out = await client.post("/patient-access/logout")
    assert out.status_code == 200
    async with sessionmaker() as session:
        assert await patient_access.find_patient_session(session, raw) is None
    # The access leg dies WITH the row (its `sid`), not whenever its 30 minutes run out.
    assert (
        await client.get("/patient-access/threads", headers=_bearer(token))
    ).status_code == 401
    # Idempotent: a second logout with nothing to revoke is still a clean 200.
    assert (await client.post("/patient-access/logout")).status_code == 200


@pytest.mark.parametrize("claim", ["sid", "login_sid"])
async def test_a_patient_token_without_its_session_claims_is_refused(pclient, claim):
    """A pre-`sid` token (or a hand-rolled one) names no row, so nothing could revoke it:
    it opens nothing. The cost is one re-login for whoever is mid-session at deploy."""
    from brain_api.config import get_settings

    client, sessionmaker, seed = pclient
    body = (await _login(client, sessionmaker, seed.both)).json()
    claims = decode_token(body["access_token"])
    claims.pop(claim)
    legacy = jwt.encode(claims, get_settings().SECRET_KEY, algorithm=ALGORITHM)
    assert (
        await client.get("/patient-access/threads", headers=_bearer(legacy))
    ).status_code == 401


# --- 7) The multi-clinic account: discover, confirm by name, end it whole -----------------

OTHER_EMAIL = "outra.pessoa@exemplo.com"


def _confirm_url(tenant_id) -> str:
    return f"/patient-access/siblings/{tenant_id}/confirm"


async def _confirm(client, token, tenant_id):
    return await client.post(
        _confirm_url(tenant_id), headers=_bearer(token), json={"tenant_id": str(tenant_id)}
    )


def _link_events():
    return (
        select(func.count())
        .select_from(PatientConsentEvent)
        .where(PatientConsentEvent.kind == CONSENT_KIND_ACCOUNT_LINK)
    )


async def _two_clinic_account(client, sessionmaker, seed):
    """The address proven at BOTH open clinics, with the login at `both` LAST.

    The order mirrors a real browser: `__Host-patient_session` is one flat cookie, so the
    jar carries whichever login came last — the login a confirmation must be bound to.
    Returns `(login, sibling_own_login)` as the two `verify-otp` bodies.
    """
    sibling = (await _login(client, sessionmaker, seed.only_secretaria)).json()
    login = (await _login(client, sessionmaker, seed.both)).json()
    return login, sibling


async def _seed_identity(sessionmaker, tenant_id, email=PATIENT_EMAIL):
    """An identity as a past verified login leaves it.

    For the cases a live login cannot produce — a clinic with the channel off issues no
    code — standing in for "this address verified there while the channel was on".
    """
    async with sessionmaker() as session, session.begin():
        patient = MessagePatient(tenant_id=tenant_id, email=email)
        session.add(patient)
        await session.flush()
        return patient.id


async def test_a_clinic_with_the_channel_off_is_never_a_candidate(pclient):
    client, sessionmaker, seed = pclient
    await _seed_identity(sessionmaker, seed.channel_off)
    login = (await _login(client, sessionmaker, seed.both)).json()
    assert login["sibling_candidates"] == []

    resp = await _confirm(client, login["access_token"], seed.channel_off)
    assert resp.status_code == 404


async def test_a_clinic_the_address_never_verified_at_is_never_a_candidate(pclient):
    """Discovery finds identities that exist; it never creates one — nor does confirm."""
    client, sessionmaker, seed = pclient
    # `only_secretaria` is open and sells secretarIA, but this address never verified there.
    login = (await _login(client, sessionmaker, seed.both)).json()
    assert login["sibling_candidates"] == []

    resp = await _confirm(client, login["access_token"], seed.only_secretaria)
    assert resp.status_code == 404
    async with sessionmaker() as session:
        rows = (
            await session.scalars(
                select(MessagePatient).where(MessagePatient.tenant_id == seed.only_secretaria)
            )
        ).all()
    assert rows == [], "neither discovery nor a confirm attempt may mint an identity"


@pytest.mark.parametrize("reason", ["another_address", "not_in_account", "unknown_clinic"])
async def test_confirm_without_a_matching_candidate_is_refused(pclient, reason):
    """One 404 for every reason — and nothing recorded, nothing minted."""
    client, sessionmaker, seed = pclient
    if reason == "another_address":
        # That clinic knows a DIFFERENT address: its patient is not this account's.
        await _seed_identity(sessionmaker, seed.only_secretaria, email=OTHER_EMAIL)
    if reason == "not_in_account":
        # Same address, no gesture at that clinic: an e-mail match is not membership.
        await _seed_identity(sessionmaker, seed.only_secretaria)
    login = (await _login(client, sessionmaker, seed.both)).json()
    target = {
        "another_address": seed.only_secretaria,
        "not_in_account": seed.only_secretaria,
        "unknown_clinic": uuid.uuid4(),
    }[reason]

    resp = await _confirm(client, login["access_token"], target)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "sibling_not_found"
    async with sessionmaker() as session:
        assert await session.scalar(_link_events()) == 0
        assert (
            await session.scalar(select(func.count()).select_from(MessagePatientSession)) == 1
        ), "a refused confirmation must not mint a session"


async def test_a_sibling_token_opens_that_clinic_and_only_that_clinic(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, payload={"status": "queued"})
    login, sibling = await _two_clinic_account(client, sessionmaker, seed)
    linked = (await _confirm(client, login["access_token"], seed.only_secretaria)).json()
    headers = _bearer(linked["access_token"])

    threads = await client.get("/patient-access/threads", headers=headers)
    assert threads.status_code == 200
    assert threads.json()["data"] == [
        {"product": "secretaria", "clinic_name": CLINIC_ONLY_SECRETARIA}
    ]
    # That clinic bought no PreCheck: the linked token does not borrow the login's products.
    refused = await client.post(
        "/patient-access/threads/precheck/messages", headers=headers, json={"text": "oi"}
    )
    assert refused.status_code == 403
    assert calls == []

    sent = await client.post(
        "/patient-access/threads/secretaria/messages", headers=headers, json={"text": "oi"}
    )
    assert sent.status_code == 200, sent.text
    assert calls[0]["json"]["tenant_id"] == str(seed.only_secretaria)
    assert calls[0]["json"]["external_id"] == sibling["patient_ref"]

    # Re-signing its tenant to the login's clinic gets nowhere: the row it names says B.
    linked_claims = decode_token(linked["access_token"])
    forged = create_patient_token(
        tenant_id=str(seed.both),
        patient_ref=sibling["patient_ref"],
        session_id=linked_claims["sid"],
        login_session_id=linked_claims["login_sid"],
    )
    assert (
        await client.get("/patient-access/threads", headers=_bearer(forged))
    ).status_code == 401


async def test_logout_with_a_bearer_ends_every_session_of_the_account(pclient):
    client, sessionmaker, seed = pclient
    # Someone ELSE at the login's clinic — same tenant, another address — must survive:
    # the account is the address, never the clinic.
    bystander = (await _login(client, sessionmaker, seed.both, email=OTHER_EMAIL)).json()
    login, sibling = await _two_clinic_account(client, sessionmaker, seed)
    linked = (await _confirm(client, login["access_token"], seed.only_secretaria)).json()

    out = await client.post("/patient-access/logout", headers=_bearer(login["access_token"]))
    assert out.status_code == 200

    account = {login["patient_ref"], sibling["patient_ref"]}
    async with sessionmaker() as session:
        rows = (await session.scalars(select(MessagePatientSession))).all()
    mine = [r for r in rows if str(r.patient_id) in account]
    theirs = [r for r in rows if str(r.patient_id) == bystander["patient_ref"]]
    # The sibling's own earlier login and the login: a clinic token names its login's row.
    assert len(mine) == 2
    assert all(r.revoked_at is not None for r in mine)
    assert len(theirs) == 1 and theirs[0].revoked_at is None

    # Every access leg of the account dies with its row — the linked clinic's included.
    for token in (login["access_token"], linked["access_token"], sibling["access_token"]):
        assert (
            await client.get("/patient-access/threads", headers=_bearer(token))
        ).status_code == 401
    assert (
        await client.get("/patient-access/threads", headers=_bearer(bystander["access_token"]))
    ).status_code == 200


async def test_logout_without_a_bearer_still_ends_only_the_cookie_session(pclient):
    """The fallback, unchanged at the row level: no bearer, only this browser's session.

    What that session OPENED goes with it — the clinic linked from it is a child of this
    login (`login_sid`) — while a session the address opened elsewhere, with its own code,
    is untouched: that is the difference between this logout and the account one.
    """
    client, sessionmaker, seed = pclient
    login, sibling = await _two_clinic_account(client, sessionmaker, seed)
    linked = (await _confirm(client, login["access_token"], seed.only_secretaria)).json()

    assert (await client.post("/patient-access/logout")).status_code == 200

    login_sid = uuid.UUID(decode_token(login["access_token"])["sid"])
    async with sessionmaker() as session:
        rows = (await session.scalars(select(MessagePatientSession))).all()
    assert [r.id for r in rows if r.revoked_at is not None] == [login_sid]
    assert len(rows) == 2
    assert (
        await client.get("/patient-access/threads", headers=_bearer(linked["access_token"]))
    ).status_code == 401
    assert (
        await client.get("/patient-access/threads", headers=_bearer(sibling["access_token"]))
    ).status_code == 200


async def test_confirm_needs_the_login_cookie_not_just_a_bearer(pclient):
    """A copied access token, replayed from another browser, links nothing."""
    client, sessionmaker, seed = pclient
    login, sibling = await _two_clinic_account(client, sessionmaker, seed)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as other:
        resp = await _confirm(other, login["access_token"], seed.only_secretaria)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "reauthentication_required"

    # Nor can a session that is not the one in this browser's cookie (here the sibling's
    # own earlier login) confirm anything.
    resp = await _confirm(client, sibling["access_token"], seed.both)
    assert resp.status_code == 401
    async with sessionmaker() as session:
        assert await session.scalar(_link_events()) == 0


@pytest.mark.parametrize("body_kind", ["another_clinic", "extra_field", "no_body"])
async def test_confirm_body_must_name_the_same_clinic_and_nothing_else(pclient, body_kind):
    client, sessionmaker, seed = pclient
    login, _ = await _two_clinic_account(client, sessionmaker, seed)
    body = {
        "another_clinic": {"tenant_id": str(seed.channel_off)},
        "extra_field": {"tenant_id": str(seed.only_secretaria), "email": PATIENT_EMAIL},
        "no_body": None,
    }[body_kind]
    resp = await client.post(
        _confirm_url(seed.only_secretaria), headers=_bearer(login["access_token"]), json=body
    )
    assert resp.status_code == 422
    async with sessionmaker() as session:
        assert await session.scalar(_link_events()) == 0


async def test_confirm_rate_limit_is_per_account_not_per_ip(pclient, monkeypatch):
    """Keyed by the authenticated address: a fresh X-Forwarded-For buys nothing, and
    another address keeps its own budget."""
    from brain_api.api import patient_access as router_mod

    client, sessionmaker, seed = pclient
    login, _ = await _two_clinic_account(client, sessionmaker, seed)
    router_mod._link_limiter._hits.clear()
    monkeypatch.setattr(router_mod._link_limiter, "_limit_getter", lambda: 1)

    first = await _confirm(client, login["access_token"], seed.only_secretaria)
    assert first.status_code == 200, first.text
    spoofed = await client.post(
        _confirm_url(seed.only_secretaria),
        headers={**_bearer(login["access_token"]), "X-Forwarded-For": "198.51.100.7"},
        json={"tenant_id": str(seed.only_secretaria)},
    )
    assert spoofed.status_code == 429
    assert router_mod._link_limiter.allow(OTHER_EMAIL), "another account keeps its budget"


async def test_a_bearer_without_the_cookie_cannot_spend_the_link_budget(pclient, monkeypatch):
    """The budget is counted only after the cookie and recency checks, so a leaked token
    replayed from another browser cannot lock the real patient out of linking."""
    from brain_api.api import patient_access as router_mod

    client, sessionmaker, seed = pclient
    login, _ = await _two_clinic_account(client, sessionmaker, seed)
    router_mod._link_limiter._hits.clear()
    monkeypatch.setattr(router_mod._link_limiter, "_limit_getter", lambda: 1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as other:
        for _ in range(3):
            replayed = await _confirm(other, login["access_token"], seed.only_secretaria)
            assert replayed.status_code == 401
    mine = await _confirm(client, login["access_token"], seed.only_secretaria)
    assert mine.status_code == 200, mine.text


# --- 8) Silent renewal: the cookie alone reopens the account -------------------------------
#
# The symptom this closes: reload the portal and you are asked for a code again, because
# the access token lived only in memory and nothing turned the cookie back into one. Every
# test below is one of the ways the fix could have re-broken something that already worked
# — a linked clinic dying with the login's row id, concurrent polling reading as theft, or
# a first-time link becoming impossible on a long session.


_CLIENT_HEADER = {CLIENT_HEADER_NAME: CLIENT_HEADER_VALUE}


async def _refresh(client, cookie=None, headers=None):
    """`POST /refresh` as the portal sends it. With `cookie`, that exact value is sent
    instead of the jar's — the way to present a value the browser was told to drop."""
    hdrs = dict(_CLIENT_HEADER if headers is None else headers)
    if cookie is not None:
        hdrs["Cookie"] = f"{PATIENT_SESSION_COOKIE_NAME}={cookie}"
    return await client.post("/patient-access/refresh", headers=hdrs)


def _threads(client, token):
    return client.get("/patient-access/threads", headers=_bearer(token))


async def _session_row(sessionmaker, token):
    async with sessionmaker() as session:
        return await session.get(MessagePatientSession, uuid.UUID(decode_token(token)["sid"]))


async def test_the_cookie_alone_reopens_the_login_clinic(pclient):
    """No bearer, no code: the cookie becomes a working access token for the same row."""
    client, sessionmaker, seed = pclient
    login = (await _login(client, sessionmaker, seed.both)).json()
    first_cookie = client.cookies.get(PATIENT_SESSION_COOKIE_NAME)
    assert first_cookie

    resp = await _refresh(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == str(seed.both)
    assert body["patient_ref"] == login["patient_ref"]
    assert body["clinic_name"] == CLINIC_BOTH
    assert [s["tenant_id"] for s in body["linked_sessions"]] == [str(seed.both)]
    # (No "token differs from the login token" check: a JWT is a pure function of its
    # claims, and a refresh inside the same second as the login legitimately yields the
    # same bytes. What matters is below: the row, the rotation, and that it opens.)
    claims = decode_token(body["access_token"])
    assert claims["scope"] == PATIENT_TOKEN_SCOPE
    # The SAME session row — the id every token of the account is bound to did not move.
    assert claims["sid"] == decode_token(login["access_token"])["sid"]
    assert claims["login_sid"] == claims["sid"]

    # The cookie rotated: a new value, the old hash parked as "previous".
    second_cookie = resp.cookies.get(PATIENT_SESSION_COOKIE_NAME)
    assert second_cookie and second_cookie != first_cookie
    row = await _session_row(sessionmaker, body["access_token"])
    assert row.token_hash == hash_refresh_token(second_cookie)
    assert row.previous_token_hash == hash_refresh_token(first_cookie)
    assert row.rotated_at is not None and row.revoked_at is None

    assert (await _threads(client, body["access_token"])).status_code == 200
    # Nothing was revoked: a token minted before the refresh is still good.
    assert (await _threads(client, login["access_token"])).status_code == 200


async def test_the_session_slides_to_ninety_days_on_every_refresh(pclient):
    """The ceiling is 90 days from the LAST refresh, never from the login (§2.1, §2.2)."""
    from brain_api.config import get_settings

    assert get_settings().PATIENT_SESSION_EXPIRE_DAYS == 90
    client, sessionmaker, seed = pclient
    login = (await _login(client, sessionmaker, seed.both)).json()
    sid = uuid.UUID(decode_token(login["access_token"])["sid"])
    # A session opened 80 days ago, 10 short of the ceiling.
    async with sessionmaker() as session, session.begin():
        row = await session.get(MessagePatientSession, sid)
        row.created_at = datetime.now(UTC) - timedelta(days=80)
        row.expires_at = datetime.now(UTC) + timedelta(days=10)

    resp = await _refresh(client)
    assert resp.status_code == 200, resp.text
    async with sessionmaker() as session:
        row = await session.get(MessagePatientSession, sid)
    assert patient_access._as_utc(row.expires_at) > datetime.now(UTC) + timedelta(
        days=89, hours=23
    )
    # `created_at` did NOT get younger: the proof of the code is what it always was.
    assert patient_access._as_utc(row.created_at) < datetime.now(UTC) - timedelta(days=79)
    # The browser's copy slides with it, with every hardening attribute intact.
    set_cookie = resp.headers["set-cookie"].lower()
    assert PATIENT_SESSION_COOKIE_NAME.lower() in set_cookie
    assert f"max-age={90 * 86400}" in set_cookie
    assert "httponly" in set_cookie and "secure" in set_cookie and "samesite=lax" in set_cookie


async def test_refresh_needs_the_client_header_before_spending_the_cookie(pclient):
    """The cookie is ambient, so the CSRF guard runs first — a forged POST burns nothing."""
    client, sessionmaker, seed = pclient
    await _login(client, sessionmaker, seed.both)
    before = client.cookies.get(PATIENT_SESSION_COOKIE_NAME)

    resp = await client.post("/patient-access/refresh")  # jar cookie, no header
    assert resp.status_code == 403
    assert resp.json()["detail"] == "missing_client_header"
    assert "set-cookie" not in resp.headers
    async with sessionmaker() as session:
        row = await session.scalar(
            select(MessagePatientSession).where(
                MessagePatientSession.token_hash == hash_refresh_token(before)
            )
        )
    assert row is not None and row.previous_token_hash is None, "the cookie was spent"
    # Not spent: the same value still renews.
    assert (await _refresh(client)).status_code == 200


@pytest.mark.parametrize("reason", ["no_cookie", "unknown", "logged_out"])
async def test_a_missing_or_dead_cookie_is_refused_and_expired(pclient, reason):
    client, sessionmaker, seed = pclient
    if reason == "no_cookie":
        resp = await _refresh(client)
        assert resp.status_code == 401
        assert "set-cookie" not in resp.headers, "nothing to expire when nothing was sent"
        return
    await _login(client, sessionmaker, seed.both)
    cookie = client.cookies.get(PATIENT_SESSION_COOKIE_NAME)
    if reason == "logged_out":
        await client.post("/patient-access/logout")
    else:
        cookie = "not-a-session-anyone-issued"
    client.cookies.clear()

    resp = await _refresh(client, cookie=cookie)
    assert resp.status_code == 401
    # The dead cookie is expired in the browser so the next boot does not start doomed.
    set_cookie = resp.headers["set-cookie"].lower()
    assert PATIENT_SESSION_COOKIE_NAME.lower() in set_cookie and "max-age=0" in set_cookie


async def test_two_refreshes_with_the_same_cookie_do_not_lock_the_patient_out(pclient):
    """Risk B, sequentially. The portal polls several threads and clinics at once, so a
    second renewal carrying the value a first one just replaced is a request that was in
    flight — NOT reuse. Inside the grace window it gets a token and no new cookie; the
    account stays whole. (On Postgres, `FOR UPDATE` turns the truly parallel case into
    exactly this sequence: the second request waits, re-reads, and lands in the grace
    branch — `rotate_patient_session`.)"""
    client, sessionmaker, seed = pclient
    login, sibling = await _two_clinic_account(client, sessionmaker, seed)
    linked = (await _confirm(client, login["access_token"], seed.only_secretaria)).json()
    t0 = client.cookies.get(PATIENT_SESSION_COOKIE_NAME)
    client.cookies.clear()

    first = await _refresh(client, cookie=t0)
    assert first.status_code == 200, first.text
    t1 = first.cookies.get(PATIENT_SESSION_COOKIE_NAME)
    assert t1 and t1 != t0

    second = await _refresh(client, cookie=t0)  # the same value, again, moments later
    assert second.status_code == 200, second.text
    assert "set-cookie" not in second.headers, "the browser already holds t1 — do not race it"
    assert decode_token(second.json()["access_token"])["sid"] == decode_token(
        login["access_token"]
    )["sid"]
    assert (await _threads(client, second.json()["access_token"])).status_code == 200
    # The linked clinic came back on BOTH answers.
    assert [s["tenant_id"] for s in second.json()["linked_sessions"]] == [
        str(seed.both),
        str(seed.only_secretaria),
    ]

    # Nothing was revoked, and the newest cookie renews normally afterwards.
    row = await _session_row(sessionmaker, login["access_token"])
    assert row.revoked_at is None
    assert row.token_hash == hash_refresh_token(t1)
    assert (await _threads(client, linked["access_token"])).status_code == 200
    third = await _refresh(client, cookie=t1)
    assert third.status_code == 200, third.text
    assert third.cookies.get(PATIENT_SESSION_COOKIE_NAME) not in (None, t0, t1)


async def test_two_parallel_refreshes_with_the_same_cookie_both_succeed(pclient):
    """Risk B, actually in parallel: two renewals fired together with one cookie both come
    back 200, and the account is alive afterwards. Whichever one rotated, the other landed
    in the grace branch; the browser is left holding a value the server knows."""
    client, sessionmaker, seed = pclient
    login = (await _login(client, sessionmaker, seed.both)).json()
    t0 = client.cookies.get(PATIENT_SESSION_COOKIE_NAME)
    client.cookies.clear()

    a, b = await asyncio.gather(_refresh(client, cookie=t0), _refresh(client, cookie=t0))
    assert (a.status_code, b.status_code) == (200, 200), (a.text, b.text)
    issued = [r.cookies.get(PATIENT_SESSION_COOKIE_NAME) for r in (a, b)]
    issued = [c for c in issued if c]
    assert issued, "at least one of them rotated"
    row = await _session_row(sessionmaker, login["access_token"])
    assert row.revoked_at is None, "two renewals in flight must never read as theft"
    # Every cookie a browser could have ended up with still opens the account.
    for cookie in issued:
        assert (await _refresh(client, cookie=cookie)).status_code == 200


async def test_the_replaced_cookie_is_theft_once_the_window_closes(pclient):
    """Risk B's other half, kept from the staff route: a value this browser was told to
    drop, presented again after the grace window, ends the WHOLE account — every clinic,
    every token — and expires the cookie. The patient re-proves the inbox."""
    from brain_api.config import get_settings

    client, sessionmaker, seed = pclient
    login, sibling = await _two_clinic_account(client, sessionmaker, seed)
    linked = (await _confirm(client, login["access_token"], seed.only_secretaria)).json()
    t0 = client.cookies.get(PATIENT_SESSION_COOKIE_NAME)

    first = await _refresh(client)
    assert first.status_code == 200
    t1 = first.cookies.get(PATIENT_SESSION_COOKIE_NAME)
    grace = get_settings().PATIENT_SESSION_ROTATION_GRACE_SECONDS
    async with sessionmaker() as session, session.begin():
        row = await session.get(
            MessagePatientSession, uuid.UUID(decode_token(login["access_token"])["sid"])
        )
        row.rotated_at = datetime.now(UTC) - timedelta(seconds=grace + 1)
    client.cookies.clear()

    replay = await _refresh(client, cookie=t0)
    assert replay.status_code == 401
    assert "max-age=0" in replay.headers["set-cookie"].lower()
    # The account is gone: the login, the fresh token, the linked clinic, the sibling's
    # own login at its clinic — the address is the account.
    for token in (
        login["access_token"],
        first.json()["access_token"],
        linked["access_token"],
        first.json()["linked_sessions"][0]["access_token"],
        sibling["access_token"],
    ):
        assert (await _threads(client, token)).status_code == 401
    assert (await _refresh(client, cookie=t1)).status_code == 401


async def test_refresh_logs_ids_only(pclient, caplog, capsys):
    """Neither cookie value nor the address may reach a log line — same rule as the code.

    Two captures: `caplog` for stdlib records (the driver's DEBUG echo of bound parameters
    included), `capsys` for structlog's console output, which does not pass through stdlib
    logging in this app."""
    client, sessionmaker, seed = pclient
    await _login(client, sessionmaker, seed.both)
    t0 = client.cookies.get(PATIENT_SESSION_COOKIE_NAME)
    capsys.readouterr()  # drop the login's output; only the refresh is under test
    with caplog.at_level(logging.DEBUG):
        resp = await _refresh(client)
    assert resp.status_code == 200
    t1 = resp.cookies.get(PATIENT_SESSION_COOKIE_NAME)
    console = capsys.readouterr().out
    everything = console + "\n".join(r.getMessage() for r in caplog.records)
    assert t0 not in everything and t1 not in everything, "a session token leaked into a log"
    app_logs = console + "\n".join(
        r.getMessage()
        for r in caplog.records
        if not r.name.startswith(("aiosqlite", "sqlalchemy"))
    )
    assert PATIENT_EMAIL not in app_logs
    assert "patient_session_refreshed" in console
