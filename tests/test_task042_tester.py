# ruff: noqa: F811 - `pclient` is a pytest fixture imported from test_patient_access
"""TASK-042 independent tester: empty-visit retention + the typed-e-mail login barrier.

Behaviour against the owner's spec, not the implementation. Complements (does not repeat)
`tests/test_visit_retention.py`. Gaps aimed at here:

- the REAL `message_switchboard.discard_empty_visit` client (wire shape + every answer),
  driven through the job, instead of a faked answer;
- an e-mail typed (or a login created) WHILE secretarIA is answering "discarded";
- visits created by the real `POST /patient-access/pending` route (the filter must fit what
  production actually writes);
- age boundaries (24 h exactly, not expired), sibling visits, the typed-typo overwrite;
- the login barrier: a typed e-mail without a valid code opens no account route.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from brain_api.core.cookies import (
    CLIENT_HEADER_NAME,
    CLIENT_HEADER_VALUE,
    PATIENT_SESSION_COOKIE_NAME,
)
from brain_api.core.database import Base
from brain_api.models import Tenant
from brain_api.models.patient_access import (
    MessagePatient,
    MessagePatientAccount,
    MessagePatientAccountOtp,
    MessagePatientSession,
    MessagePendingSession,
)
from brain_api.services import message_switchboard
from brain_api.services.portal import visit_retention
from tests.test_patient_access import (  # noqa: F401 - pclient is a pytest fixture
    PATIENT_EMAIL,
    _bearer,
    _configure_mesh,
    _peek_code,
    pclient,
)
from tests.test_patient_pending_session import _claim, _open

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
TYPED = "digitado@example.com"
OTHER = "outro@example.com"


# --- scaffolding -----------------------------------------------------------------------------


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _visit(maker, *, age_hours: float, expired: bool = True, **pending) -> tuple[UUID, UUID]:
    """An otherwise-empty visit. Returns (patient_id, tenant_id)."""
    created = NOW - timedelta(hours=age_hours)
    async with maker() as session:
        tenant = Tenant(clinic_name="Clinic")
        session.add(tenant)
        await session.flush()
        patient = MessagePatient(id=uuid4(), tenant_id=tenant.id, created_at=created)
        session.add(patient)
        await session.flush()
        session.add(
            MessagePendingSession(
                patient_id=patient.id,
                tenant_id=tenant.id,
                token_hash=uuid4().hex,
                created_at=created,
                expires_at=created + timedelta(hours=24) if expired else NOW + timedelta(days=1),
                **pending,
            )
        )
        await session.commit()
        return patient.id, tenant.id


async def _count(maker, model, *where) -> int:
    async with maker() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where))


async def _run(maker, **kw):
    return await visit_retention.run_retention_round(maker, now=kw.pop("now", NOW), dry_run=False)


class _Reply:
    """Just enough of `httpx.Response` for `message_switchboard.discard_empty_visit`."""

    def __init__(self, status_code=200, payload=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def wire(monkeypatch):
    """Drive the REAL discard client: record the HTTP hop, answer whatever the test says."""
    _configure_mesh(monkeypatch)
    state = type(
        "Wire", (), {"reply": _Reply(200, {"status": "discarded"}), "hops": [], "boom": None}
    )()
    real_request = httpx.AsyncClient.request

    async def _fake(self, method, url, *, headers=None, json=None, **kw):
        if "secretaria:8000" not in str(self.base_url):
            return await real_request(self, method, url, headers=headers, json=json, **kw)
        state.hops.append(
            {"method": method, "url": str(url), "headers": dict(headers or {}), "json": json}
        )
        if state.boom is not None:
            raise state.boom
        return state.reply

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake)
    return state


@pytest.fixture
def secretaria(monkeypatch):
    """Faked `discard_empty_visit` for the tests that need a callback in the middle."""
    fake = type("Fake", (), {"answer": "discarded", "asked": [], "during": None})()

    async def _discard(*, tenant_id, visit_ref):
        fake.asked.append((tenant_id, visit_ref))
        if fake.during is not None:
            await fake.during(visit_ref)
        return fake.answer

    monkeypatch.setattr(message_switchboard, "discard_empty_visit", _discard)
    return fake


# --- (a)/(b) age, both sides, and what goes over the wire -------------------------------------


async def test_a_25h_empty_visit_is_gone_on_both_sides_and_secretaria_got_the_exact_call(
    maker, wire
) -> None:
    """(a) The real client sends the frozen contract; a `discarded` answer clears brain-api."""
    patient_id, tenant_id = await _visit(maker, age_hours=25)

    result = await _run(maker)

    assert result.discarded == 1
    assert len(wire.hops) == 1
    hop = wire.hops[0]
    assert hop["method"] == "POST"
    assert hop["url"] == "/internal/brain-message/visits/discard"
    assert hop["headers"]["X-Internal-Api-Key"] == "secretaria-key-AAA"
    assert hop["json"] == {"tenant_id": str(tenant_id), "external_id": str(patient_id)}
    assert await _count(maker, MessagePendingSession) == 0
    assert await _count(maker, MessagePatient) == 0


async def test_b_exactly_24h_old_and_a_25h_visit_whose_cookie_is_still_alive_both_stay(
    maker, wire
) -> None:
    """(b) The bound is strict, and a still-live cookie (not expired) protects a >24 h visit."""
    await _visit(maker, age_hours=24)  # expires exactly NOW: not yet expired
    await _visit(maker, age_hours=23.9)
    await _visit(maker, age_hours=25, expired=False)

    result = await _run(maker)

    assert result.candidates == 0
    assert wire.hops == []
    assert await _count(maker, MessagePatient) == 3


async def test_a_visit_past_24h_plus_the_expiry_grace_goes_and_inside_the_grace_stays(
    maker, wire
) -> None:
    """(b, other side of the bound) the job leaves a freshly-expired cookie a short grace."""
    await _visit(maker, age_hours=24 + 5 / 60)  # expired 5 min ago: inside the grace
    assert (await _run(maker)).candidates == 0
    await _visit(maker, age_hours=24 + 20 / 60)  # expired 20 min ago
    assert (await _run(maker)).discarded == 1
    assert await _count(maker, MessagePatient) == 1


async def test_a_lost_reply_is_healed_next_round_without_orphaning_brain_api(maker, wire) -> None:
    """secretarIA applied the discard but the answer was lost; next round it says `absent`."""
    await _visit(maker, age_hours=30)
    wire.boom = httpx.ReadTimeout("lost reply")
    assert (await _run(maker)).failed == 1
    assert await _count(maker, MessagePatient) == 1

    wire.boom = None
    wire.reply = _Reply(200, {"status": "absent"})
    healed = await _run(maker)

    assert healed.discarded == 1
    assert await _count(maker, MessagePatient) == 0
    assert await _count(maker, MessagePendingSession) == 0


async def test_a_precheck_clinic_visit_is_cleaned_only_if_the_patient_never_typed(
    pclient, secretaria, monkeypatch
) -> None:
    """Owner, 2026-10-09: PreCheck's own greeting is not the patient; a typed message is.

    Three real visits at a clinic with PreCheck, opened through the real route: one never
    typed (cleaned), one typed in the PreCheck tab (kept), one typed in the secretarIA tab
    (kept). The stamp is set by the relay of the PATIENT's message, whichever product.
    """
    from tests.test_patient_access import _bearer, _configure_mesh, _spy_transport

    client, sessionmaker, seed = pclient
    opened = []
    for _ in range(3):
        client.cookies.clear()  # a fresh browser each time, or the visit is resumed
        opened.append((await _open(client, sessionmaker, seed.both)).json())
    silent, typed_precheck, typed_secretaria = opened
    assert len({o["patient_ref"] for o in opened}) == 3
    _configure_mesh(monkeypatch)
    _spy_transport(monkeypatch)
    for opened, product in ((typed_precheck, "precheck"), (typed_secretaria, "secretaria")):
        resp = await client.post(
            f"/patient-access/threads/{product}/messages",
            headers=_bearer(opened["pending_token"]),
            json={"text": "oi"},
        )
        assert resp.status_code == 200, resp.text

    result = await visit_retention.run_retention_round(
        sessionmaker, now=datetime.now(UTC) + timedelta(days=3), dry_run=False
    )

    assert result.precheck_clinics == 1
    assert secretaria.asked == [(seed.both, silent["patient_ref"])]
    assert result.discarded == 1
    for kept in (typed_precheck, typed_secretaria):
        assert (
            await _count(
                sessionmaker, MessagePatient, MessagePatient.id == UUID(kept["patient_ref"])
            )
            == 1
        )


# --- (c)/(f) every secretarIA answer, through the real client ----------------------------------


@pytest.mark.parametrize(
    ("reply", "kept_field"),
    [
        (_Reply(409, {"detail": "visit_not_empty"}), "kept_not_empty"),
        (_Reply(200, {"status": "absent"}), "kept_absent"),
    ],
)
async def test_c_a_refusal_or_absence_keeps_the_visit_for_good_and_never_asks_again(
    maker, wire, reply, kept_field
) -> None:
    wire.reply = reply
    await _visit(maker, age_hours=30)

    first = await _run(maker)
    second = await _run(maker)

    assert getattr(first, kept_field) == 1
    assert first.discarded == 0
    assert second.candidates == 0
    assert len(wire.hops) == 1, "a kept visit is never asked about again"
    assert await _count(maker, MessagePatient) == 1
    assert await _count(maker, MessagePendingSession) == 1
    async with maker() as session:
        assert (await session.scalar(select(MessagePendingSession.retention_kept_at))) is not None


@pytest.mark.parametrize(
    "reply",
    [
        _Reply(500, {"detail": "boom"}),
        _Reply(502),
        _Reply(404, {"detail": "Not Found"}),  # secretarIA without the route yet
        _Reply(401, {"detail": "bad key"}),
        _Reply(422, {"detail": []}),
        _Reply(200, {"status": "weird"}),
        _Reply(200, {}),
        _Reply(200, bad_json=True),
    ],
)
async def test_f_any_other_answer_deletes_nothing_and_retries_next_round(
    maker, wire, reply
) -> None:
    wire.reply = reply
    await _visit(maker, age_hours=30)

    first = await _run(maker)

    assert first.failed == 1
    assert first.discarded == 0
    assert await _count(maker, MessagePatient) == 1
    assert await _count(maker, MessagePendingSession) == 1
    async with maker() as session:
        assert (await session.scalar(select(MessagePendingSession.retention_kept_at))) is None

    wire.reply = _Reply(200, {"status": "discarded"})
    assert (await _run(maker)).discarded == 1, "the failure is retried, not remembered"


@pytest.mark.parametrize(
    "boom", [httpx.ConnectError("down"), httpx.ReadTimeout("slow"), OSError("x")]
)
async def test_f_a_network_failure_deletes_nothing(maker, wire, boom) -> None:
    wire.boom = boom
    await _visit(maker, age_hours=30)

    result = await _run(maker)

    assert result.failed == 1
    assert await _count(maker, MessagePatient) == 1
    assert await _count(maker, MessagePendingSession) == 1


async def test_f_a_200_whose_body_is_not_an_object_is_a_failure_not_a_crash(maker, wire) -> None:
    """`discard_empty_visit` promises "never raises". A JSON list/string/null must not escape."""
    await _visit(maker, age_hours=30)
    for body in ([], "discarded", None, 7):
        wire.reply = _Reply(200, body)
        result = await _run(maker)
        assert result.failed == 1, body
        assert await _count(maker, MessagePatient) == 1


async def test_f_an_unconfigured_secretaria_deletes_nothing(maker, monkeypatch) -> None:
    from brain_api.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "SECRETARIA_BASE_URL", "")
    monkeypatch.setattr(settings, "SECRETARIA_API_KEY", "")
    await _visit(maker, age_hours=30)
    await _visit(maker, age_hours=30)

    result = await _run(maker)

    assert result.discarded == 0
    assert result.failed >= 1
    assert await _count(maker, MessagePatient) == 2


# --- (d) a typed e-mail is never deleted -------------------------------------------------------


@pytest.mark.parametrize("age_hours", [25, 24 * 30, 24 * 365, 24 * 365 * 5])
@pytest.mark.parametrize("shape", ["email_only", "claimed_only", "both", "identity_email"])
async def test_d_a_typed_unverified_email_is_never_deleted_at_any_age(
    maker, secretaria, shape, age_hours
) -> None:
    created = NOW - timedelta(hours=age_hours)
    extra = {
        "email_only": {"email": TYPED},
        "claimed_only": {"claimed_at": created},
        "both": {"email": TYPED, "claimed_at": created},
        "identity_email": {},
    }[shape]
    patient_id, _ = await _visit(maker, age_hours=age_hours, **extra)
    async with maker() as session:
        if shape == "identity_email":
            await session.execute(
                update(MessagePatient).where(MessagePatient.id == patient_id).values(email=TYPED)
            )
        session.add(
            MessagePatientAccountOtp(
                email=TYPED, code_hash="0" * 64, expires_at=created + timedelta(minutes=10)
            )
        )
        await session.commit()

    result = await _run(maker)
    again = await _run(maker, now=NOW + timedelta(days=400))

    assert secretaria.asked == [], "secretarIA must not even be asked"
    assert result.candidates == 0 == again.candidates
    assert await _count(maker, MessagePatient) == 1
    assert await _count(maker, MessagePendingSession) == 1
    assert await _count(maker, MessagePatientAccountOtp) == 1, "OTP rows are never touched"


async def test_d_control_the_same_visit_without_an_email_IS_deleted(maker, secretaria) -> None:
    """Sensitivity: only the address separates 'deleted' from 'kept' in test (d)."""
    await _visit(maker, age_hours=24 * 365)
    assert (await _run(maker)).discarded == 1


@pytest.mark.parametrize("late", ["email", "claimed_at", "verified_at"])
async def test_d_an_email_typed_while_secretaria_is_answering_still_saves_the_visit(
    maker, secretaria, late
) -> None:
    """Race: the visit looked empty, secretarIA says discarded, but the patient types an address
    (or verifies) in between. brain-api's own DELETE guard must refuse."""
    patient_id, _ = await _visit(maker, age_hours=30)

    async def _patient_types(_ref: str) -> None:
        value = {"email": TYPED, "claimed_at": NOW, "verified_at": NOW}[late]
        async with maker() as session:
            await session.execute(
                update(MessagePendingSession)
                .where(MessagePendingSession.patient_id == patient_id)
                .values(**{late: value})
            )
            await session.commit()

    secretaria.during = _patient_types

    result = await _run(maker)

    assert result.discarded == 0
    assert await _count(maker, MessagePatient) == 1
    assert await _count(maker, MessagePendingSession) == 1


async def test_d_a_login_row_created_while_secretaria_is_answering_keeps_the_identity(
    maker, secretaria
) -> None:
    patient_id, tenant_id = await _visit(maker, age_hours=30)

    async def _login(_ref: str) -> None:
        async with maker() as session:
            session.add(
                MessagePatientSession(
                    patient_id=patient_id,
                    tenant_id=tenant_id,
                    token_hash=uuid4().hex,
                    expires_at=NOW + timedelta(days=1),
                )
            )
            await session.commit()

    secretaria.during = _login
    await _run(maker)

    # Observation (tester report): the pending row is deleted and `discarded` is counted even
    # though the identity is kept; only what must never be lost is asserted here.
    assert await _count(maker, MessagePatient) == 1
    assert await _count(maker, MessagePatientSession) == 1


# --- (e) verified / promoted / account never go ----------------------------------------------


async def test_e_verified_promoted_named_or_accounted_visits_never_go(maker, secretaria) -> None:
    verified, _ = await _visit(maker, age_hours=48, verified_at=NOW - timedelta(hours=47))
    accounted, _ = await _visit(maker, age_hours=48)
    named, _ = await _visit(maker, age_hours=48)
    superseded_target, _ = await _visit(maker, age_hours=48)
    superseding, _ = await _visit(maker, age_hours=48, superseded_by=None)
    otp_asked, _ = await _visit(maker, age_hours=48, otp_requested_at=NOW - timedelta(hours=47))
    async with maker() as session:
        account = MessagePatientAccount(email="conta@example.com")
        session.add(account)
        await session.flush()
        await session.execute(
            update(MessagePatient)
            .where(MessagePatient.id == accounted)
            .values(account_id=account.id)
        )
        await session.execute(
            update(MessagePatient).where(MessagePatient.id == named).values(name="Maria")
        )
        # the visit that was merged INTO another identity (promotion)
        await session.execute(
            update(MessagePendingSession)
            .where(MessagePendingSession.patient_id == superseding)
            .values(superseded_by=superseded_target)
        )
        await session.commit()

    result = await _run(maker)

    assert result.candidates == 0, "nothing in this set is an empty visit"
    assert secretaria.asked == []
    assert await _count(maker, MessagePatient) == 6
    assert await _count(maker, MessagePatientAccount) == 1
    _ = (verified, otp_asked)


# --- (g) idempotence and unrelated rows -------------------------------------------------------


async def test_g_two_rounds_delete_once_and_leave_neighbours_alone(maker, secretaria) -> None:
    await _visit(maker, age_hours=30)
    keep_young, _ = await _visit(maker, age_hours=2)
    keep_typed, _ = await _visit(maker, age_hours=30, email=TYPED, claimed_at=NOW)

    first = await _run(maker)
    second = await _run(maker)

    assert (first.discarded, second.discarded, second.candidates) == (1, 0, 0)
    assert len(secretaria.asked) == 1
    remaining = set(await _ids(maker))
    assert remaining == {keep_young, keep_typed}


async def _ids(maker):
    async with maker() as session:
        return list(await session.scalars(select(MessagePatient.id)))


async def test_g_asks_with_the_visits_own_clinic(maker, secretaria) -> None:
    patient_a, tenant_a = await _visit(maker, age_hours=30)
    patient_b, tenant_b = await _visit(maker, age_hours=31)

    await _run(maker)

    assert sorted(secretaria.asked, key=str) == sorted(
        [(tenant_a, str(patient_a)), (tenant_b, str(patient_b))], key=str
    )


# --- sibling visits on one identity -----------------------------------------------------------


async def test_a_sibling_pending_visit_on_the_same_identity_protects_it(maker, secretaria) -> None:
    patient_id, tenant_id = await _visit(maker, age_hours=48)
    async with maker() as session:
        session.add(
            MessagePendingSession(
                patient_id=patient_id,
                tenant_id=tenant_id,
                token_hash=uuid4().hex,
                created_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(hours=23),
            )
        )
        await session.commit()

    result = await _run(maker)

    assert result.candidates == 0
    assert secretaria.asked == []
    assert await _count(maker, MessagePatient) == 1
    assert await _count(maker, MessagePendingSession) == 2


# --- real route: the filter fits what production writes ---------------------------------------


async def test_a_visit_minted_by_the_real_open_route_is_collected_after_24h(
    pclient, secretaria
) -> None:
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.only_secretaria)).json()
    patient_ref = UUID(opened["patient_ref"])

    young = await visit_retention.run_retention_round(
        sessionmaker, now=datetime.now(UTC) + timedelta(hours=2), dry_run=False
    )
    assert young.candidates == 0

    old = await visit_retention.run_retention_round(
        sessionmaker, now=datetime.now(UTC) + timedelta(hours=49), dry_run=False
    )

    assert secretaria.asked == [(seed.only_secretaria, str(patient_ref))]
    assert old.discarded == 1
    assert await _count(sessionmaker, MessagePendingSession) == 0
    assert await _count(sessionmaker, MessagePatient, MessagePatient.id == patient_ref) == 0
    gone = await client.get(
        "/patient-access/pending/status", headers=_bearer(opened["pending_token"])
    )
    assert gone.status_code == 401, "the deleted visit's token dies with it"


async def test_i_a_typo_then_the_corrected_email_overwrites_on_the_same_visit(
    pclient, monkeypatch, secretaria
) -> None:
    """(i) One visit, one row, the last address wins - and it stays protected for ever."""
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    ref = opened["patient_ref"]

    typo = await _claim(client, monkeypatch, seed.both, ref, email="mria@example.com")
    fixed = await _claim(client, monkeypatch, seed.both, ref, email=TYPED)

    assert typo.status_code == fixed.status_code == 200, (typo.text, fixed.text)
    assert await _count(sessionmaker, MessagePendingSession) == 1
    assert await _count(sessionmaker, MessagePatient) == 1
    async with sessionmaker() as session:
        row = await session.scalar(select(MessagePendingSession))
    assert row.email == TYPED
    assert row.claimed_at is not None
    assert str(row.patient_id) == ref

    far = await visit_retention.run_retention_round(
        sessionmaker, now=datetime.now(UTC) + timedelta(days=400), dry_run=False
    )
    assert far.candidates == 0
    assert secretaria.asked == []
    assert await _count(sessionmaker, MessagePendingSession) == 1


# --- (h) login barrier -------------------------------------------------------------------------


async def test_h_a_typed_email_without_a_valid_code_hands_out_no_account_or_cookie(
    pclient, monkeypatch
) -> None:
    """(h) Claimed e-mail, then: no code / wrong code. Nothing issues an account token or cookie."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    opened = (await _open(client, sessionmaker, seed.both)).json()
    token = opened["pending_token"]
    claim = await _claim(client, monkeypatch, seed.both, opened["patient_ref"], email=PATIENT_EMAIL)
    assert claim.status_code == 200, claim.text
    assert "token" not in claim.text.lower()
    client_header = {CLIENT_HEADER_NAME: CLIENT_HEADER_VALUE}

    async def _assert_no_login(stage: str) -> None:
        refresh = await client.post("/patient-access/refresh", headers=client_header)
        assert refresh.status_code == 401, (stage, refresh.text)
        assert PATIENT_SESSION_COOKIE_NAME not in client.cookies, stage
        complete = await client.post("/patient-access/pending/complete", headers=_bearer(token))
        assert complete.status_code == 409, (stage, complete.text)
        assert PATIENT_SESSION_COOKIE_NAME not in complete.headers.get("set-cookie", ""), stage
        assert "account_token" not in complete.text, stage
        status = await client.get("/patient-access/pending/status", headers=_bearer(token))
        assert status.status_code == 200
        assert status.json()["state"] != "verified", stage
        assert await _count(sessionmaker, MessagePatientAccount) == 0, stage
        assert await _count(sessionmaker, MessagePatientSession) == 0, stage

    # 1) e-mail typed, no code requested at all.
    await _assert_no_login("claimed, no code")
    assert (await client.get("/patient-access/pending/status", headers=_bearer(token))).json()[
        "state"
    ] == "pending_claimed"

    # 2) code requested, wrong code answered (several times).
    asked = await client.post("/patient-access/pending/request-otp", headers=_bearer(token))
    assert asked.status_code == 200, asked.text
    real = await _peek_code(sessionmaker, None, PATIENT_EMAIL)
    wrong = "000000" if real != "000000" else "111111"
    for _ in range(2):
        bad = await client.post(
            "/patient-access/pending/verify-otp", headers=_bearer(token), json={"code": wrong}
        )
        assert bad.status_code == 400, bad.text
        assert "account_token" not in bad.text
        assert PATIENT_SESSION_COOKIE_NAME not in bad.headers.get("set-cookie", "")
    await _assert_no_login("code requested, wrong code")
    assert (await client.get("/patient-access/pending/status", headers=_bearer(token))).json()[
        "state"
    ] == "otp_sent"

    # 3) control: the right code DOES open it (so the refusals above are about the code).
    good = await client.post(
        "/patient-access/pending/verify-otp", headers=_bearer(token), json={"code": real}
    )
    assert good.status_code == 200, good.text
    assert "account_token" in good.json()


async def test_h_the_internal_claim_alone_never_verifies_even_for_a_known_account(
    pclient, monkeypatch
) -> None:
    """Claiming an address that ALREADY has an account must not log the visit in either."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    first = (await _open(client, sessionmaker, seed.both)).json()
    await _claim(client, monkeypatch, seed.both, first["patient_ref"])
    asked = await client.post(
        "/patient-access/pending/request-otp", headers=_bearer(first["pending_token"])
    )
    code = await _peek_code(sessionmaker, None, PATIENT_EMAIL)
    verified = await client.post(
        "/patient-access/pending/verify-otp",
        headers=_bearer(first["pending_token"]),
        json={"code": code},
    )
    assert asked.status_code == verified.status_code == 200
    await client.post("/patient-access/pending/complete", headers=_bearer(first["pending_token"]))
    client.cookies.clear()

    second = (await _open(client, sessionmaker, seed.both)).json()
    claim = await _claim(client, monkeypatch, seed.both, second["patient_ref"])
    assert claim.status_code == 200
    complete = await client.post(
        "/patient-access/pending/complete", headers=_bearer(second["pending_token"])
    )
    refresh = await client.post(
        "/patient-access/refresh", headers={CLIENT_HEADER_NAME: CLIENT_HEADER_VALUE}
    )
    state = await client.get(
        "/patient-access/pending/status", headers=_bearer(second["pending_token"])
    )

    assert complete.status_code == 409
    assert refresh.status_code == 401
    assert state.json()["state"] != "verified"
    assert PATIENT_SESSION_COOKIE_NAME not in client.cookies
