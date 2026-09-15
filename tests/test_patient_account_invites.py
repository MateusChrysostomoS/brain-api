"""The account model (2026-09-15): e-mail + code open an ACCOUNT, and a clinic enters it only
by the patient's gesture — the clinic's link on login, or its link/code pasted later.

Every test here guards one line of the prompt's checklist or one review finding
(docs/CHECKPOINT_portal_clinicas_convite.md §8–§9): the login needs no clinic; the old body is
login + invite; a link on first contact lands with the clinic; an open account adds a clinic
without a code, once; UUID, code and link name the same clinic and every bad invite gets the
same refusal; neither an e-mail match nor a separate login from before the account puts a
clinic in the account; the rows the previous deploy left behind keep working; and the races
the reviews traced (parallel guesses, parallel adds, an account created mid-login, two staff
reads) settle without a 500 or a second grant.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from brain_api.config import get_settings
from brain_api.core.cookies import PATIENT_SESSION_COOKIE_NAME
from brain_api.core.invite_codes import normalize_invite_code, parse_invite
from brain_api.core.security import (
    PATIENT_ACCOUNT_TOKEN_SCOPE,
    PATIENT_TOKEN_SCOPE,
    create_access_token,
    create_patient_token,
    decode_token,
    hash_refresh_token,
)
from brain_api.models import Tenant
from brain_api.models.patient_access import (
    CONSENT_KIND_ACCOUNT_LINK,
    CONSENT_KIND_CHANNEL_ACCESS,
    CONSENT_LEGAL_BASIS_PENDING,
    MessagePatient,
    MessagePatientAccount,
    MessagePatientAccountOtp,
    MessagePatientSession,
    PatientConsentEvent,
)
from brain_api.services import patient_access
from tests.test_patient_access import (
    CLINIC_BOTH,
    CLINIC_ONLY_SECRETARIA,
    OTHER_EMAIL,
    PATIENT_EMAIL,
    _bearer,
    _confirm,
    _login,
    _peek_code,
    _refresh,
    _seed_identity,
    _threads,
    _two_clinic_account,
)

_INVALID_CODE = "Código inválido ou expirado"


async def _request(client, email=PATIENT_EMAIL):
    resp = await client.post("/patient-access/request-otp", json={"email": email})
    assert resp.status_code == 200, resp.text


def _verify(client, code, email=PATIENT_EMAIL):
    return client.post("/patient-access/verify-otp", json={"email": email, "code": code})


async def _email_login(client, sessionmaker, email=PATIENT_EMAIL, invite=None):
    """The account login: e-mail + code, and optionally the invite the patient arrived with."""
    await _request(client, email)
    body = {"email": email, "code": await _peek_code(sessionmaker, None, email)}
    if invite is not None:
        body["invite"] = invite
    return await client.post("/patient-access/verify-otp", json=body)


async def _invite_code(sessionmaker, tenant_id) -> str:
    async with sessionmaker() as session:
        return (await session.get(Tenant, tenant_id)).patient_invite_code


def _add(client, token, invite):
    return client.post("/patient-access/clinics", headers=_bearer(token), json={"invite": invite})


async def _count(sessionmaker, model, *where) -> int:
    async with sessionmaker() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where))


async def _seed_legacy_login(sessionmaker, tenant_id, raw=None, *, linked=False):
    """An identity exactly as the previous deploy left it, with no account anywhere: the
    first-contact consent every verified code wrote, optionally a link confirmed by name, and
    optionally its own login row whose cookie is `raw`. Returns `(patient_id, row_id)`."""
    async with sessionmaker() as session, session.begin():
        patient = MessagePatient(tenant_id=tenant_id, email=PATIENT_EMAIL)
        session.add(patient)
        await session.flush()
        kinds = [CONSENT_KIND_CHANNEL_ACCESS] + ([CONSENT_KIND_ACCOUNT_LINK] if linked else [])
        for kind in kinds:
            session.add(
                PatientConsentEvent(
                    tenant_id=tenant_id,
                    subject_ref=str(patient.id),
                    kind=kind,
                    legal_basis=CONSENT_LEGAL_BASIS_PENDING,
                )
            )
        row_id = None
        if raw is not None:
            row = MessagePatientSession(
                patient_id=patient.id,
                tenant_id=tenant_id,
                token_hash=hash_refresh_token(raw),
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
            session.add(row)
            await session.flush()
            row_id = row.id
        return patient.id, row_id


# --- 1) The login needs no clinic --------------------------------------------------------


async def test_email_alone_opens_an_account_with_no_clinic(pclient):
    client, sessionmaker, seed = pclient
    resp = await _email_login(client, sessionmaker)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    claims = decode_token(body["account_token"])
    assert claims["scope"] == PATIENT_ACCOUNT_TOKEN_SCOPE
    assert "tenant_id" not in claims and "email" not in claims
    assert body["clinics"] == [] and body["invited_tenant_id"] is None
    assert body["access_token"] is None and body["tenant_id"] is None
    assert PATIENT_SESSION_COOKIE_NAME in resp.cookies
    async with sessionmaker() as session:
        account = await session.scalar(select(MessagePatientAccount))
        row = await session.get(MessagePatientSession, uuid.UUID(claims["sid"]))
    assert account.email == PATIENT_EMAIL and str(account.id) == claims["sub"]
    assert row.account_id == account.id and row.patient_id is None and row.tenant_id is None
    assert await _count(sessionmaker, MessagePatient) == 0
    assert await _count(sessionmaker, PatientConsentEvent) == 0

    # The account token opens no thread, and the cookie brings the (empty) account back.
    assert (await _threads(client, body["account_token"])).status_code == 401
    renewed = await _refresh(client)
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["clinics"] == []


async def test_the_old_body_is_login_plus_invite_of_that_clinic(pclient):
    """The portal deployed on 2026-09-14 still sends `tenant_id`; the backend deploys first."""
    client, sessionmaker, seed = pclient
    body = (await _login(client, sessionmaker, seed.both)).json()
    assert body["invited_tenant_id"] == str(seed.both)
    assert [c["tenant_id"] for c in body["clinics"]] == [str(seed.both)]
    assert body["tenant_id"] == str(seed.both)
    assert body["access_token"] == body["clinics"][0]["access_token"]
    assert decode_token(body["access_token"])["scope"] == PATIENT_TOKEN_SCOPE
    assert (await _threads(client, body["access_token"])).status_code == 200

    # A channel-off clinic in the old body fails like before — BEFORE the code is spent.
    await _request(client)
    code = await _peek_code(sessionmaker, None)
    refused = await client.post(
        "/patient-access/verify-otp",
        json={"tenant_id": str(seed.channel_off), "email": PATIENT_EMAIL, "code": code},
    )
    assert refused.status_code == 400 and refused.json()["detail"] == _INVALID_CODE
    ok = await _verify(client, code)
    assert ok.status_code == 200, "the refused old body must not have burned the code"


async def test_invite_and_tenant_id_together_are_refused(pclient):
    client, sessionmaker, seed = pclient
    resp = await client.post(
        "/patient-access/verify-otp",
        json={
            "email": PATIENT_EMAIL,
            "code": "123456",
            "tenant_id": str(seed.both),
            "invite": str(seed.both),
        },
    )
    assert resp.status_code == 422


# --- 2) First contact through the clinic's link ------------------------------------------


async def test_a_first_contact_through_the_clinic_link_lands_with_that_clinic(pclient):
    client, sessionmaker, seed = pclient
    code = await _invite_code(sessionmaker, seed.both)
    resp = await _email_login(
        client, sessionmaker, invite=f"https://portal.exemplo/clinicas/?convite={code}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["invited_tenant_id"] == str(seed.both)
    assert [c["clinic_name"] for c in body["clinics"]] == [CLINIC_BOTH]
    assert (await _threads(client, body["clinics"][0]["access_token"])).status_code == 200
    async with sessionmaker() as session:
        kinds = [e.kind for e in (await session.scalars(select(PatientConsentEvent))).all()]
    assert kinds == [CONSENT_KIND_CHANNEL_ACCESS]

    # An invite that names nothing usable does not fail a correct code.
    other = await _email_login(client, sessionmaker, email=OTHER_EMAIL, invite="nada disso")
    assert other.status_code == 200, other.text
    assert other.json()["invited_tenant_id"] is None and other.json()["clinics"] == []


# --- 3) An open account adds a clinic: no code, idempotent, one consent -------------------


async def test_an_open_account_adds_a_clinic_without_a_code_and_only_once(pclient):
    client, sessionmaker, seed = pclient
    login = (await _email_login(client, sessionmaker)).json()
    async with sessionmaker() as session:
        challenge = (await session.scalar(select(MessagePatientAccountOtp))).code_hash
    code = await _invite_code(sessionmaker, seed.only_secretaria)

    first = await _add(client, login["account_token"], code)
    assert first.status_code == 200, first.text
    again = await _add(client, login["account_token"], code.lower())
    assert again.status_code == 200, again.text
    added = first.json()
    assert added["tenant_id"] == str(seed.only_secretaria)
    assert added["clinic_name"] == CLINIC_ONLY_SECRETARIA
    assert again.json()["patient_ref"] == added["patient_ref"]
    claims = decode_token(added["access_token"])
    assert claims["sid"] == claims["login_sid"] == decode_token(login["account_token"])["sid"]

    consent = (
        PatientConsentEvent.subject_ref == added["patient_ref"],
        PatientConsentEvent.kind == CONSENT_KIND_CHANNEL_ACCESS,
    )
    assert await _count(sessionmaker, PatientConsentEvent, *consent) == 1
    async with sessionmaker() as session:
        assert (await session.scalar(select(MessagePatientAccountOtp))).code_hash == challenge
    assert await _count(sessionmaker, MessagePatientSession) == 1, "no row per clinic"
    renewed = (await _refresh(client)).json()
    assert [c["tenant_id"] for c in renewed["clinics"]] == [str(seed.only_secretaria)]
    assert renewed["tenant_id"] == str(seed.only_secretaria), "the first clinic pins the login"
    assert (await _threads(client, renewed["clinics"][0]["access_token"])).status_code == 200


async def test_an_invite_on_an_old_login_needs_no_code(pclient):
    client, sessionmaker, seed = pclient
    login = (await _email_login(client, sessionmaker)).json()
    async with sessionmaker() as session, session.begin():
        row = await session.get(
            MessagePatientSession, uuid.UUID(decode_token(login["account_token"])["sid"])
        )
        row.created_at = datetime.now(UTC) - timedelta(days=45)
    fresh = (await _refresh(client)).json()["account_token"]
    resp = await _add(client, fresh, str(seed.both))
    assert resp.status_code == 200, resp.text


async def test_parallel_adds_of_a_new_clinic_make_one_identity_and_one_consent(pclient):
    """A double (triple) tap on "Entrar também": the inserts race on the unique
    (tenant, e-mail) key. None may fail — a helper that rolled back to recover expired the
    caller's rows (a 500) — and the consent must still be written once."""
    client, sessionmaker, seed = pclient
    login = (await _email_login(client, sessionmaker)).json()
    answers = await asyncio.gather(
        *(_add(client, login["account_token"], str(seed.only_secretaria)) for _ in range(3))
    )
    assert [a.status_code for a in answers] == [200, 200, 200], [a.text for a in answers]
    assert len({a.json()["patient_ref"] for a in answers}) == 1
    at_clinic = MessagePatient.tenant_id == seed.only_secretaria
    assert await _count(sessionmaker, MessagePatient, at_clinic) == 1
    consent = PatientConsentEvent.subject_ref == answers[0].json()["patient_ref"]
    assert await _count(sessionmaker, PatientConsentEvent, consent) == 1


# --- 4) Every form of invite; one refusal for every bad one ------------------------------


@pytest.mark.parametrize("form", ["uuid", "code", "typed_code", "link", "old_link"])
async def test_uuid_code_and_links_name_the_same_clinic(pclient, form):
    client, sessionmaker, seed = pclient
    login = (await _email_login(client, sessionmaker)).json()
    code = await _invite_code(sessionmaker, seed.both)
    invite = {
        "uuid": str(seed.both),
        "code": code,
        "typed_code": f" {code[:4].lower()}-{code[4:].lower()} ",
        "link": f"https://portal.exemplo/clinicas/?convite={code}",
        "old_link": f"https://portal.exemplo/conversa/?clinica={seed.both}",
    }[form]
    resp = await _add(client, login["account_token"], invite)
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == str(seed.both)
    assert resp.json()["clinic_name"] == CLINIC_BOTH


@pytest.mark.parametrize(
    "reason", ["garbage", "unknown_uuid", "unknown_code", "channel_off_uuid", "channel_off_code"]
)
async def test_every_bad_invite_gets_the_same_refusal(pclient, reason):
    client, sessionmaker, seed = pclient
    login = (await _email_login(client, sessionmaker)).json()
    invite = {
        "garbage": "isso não é convite",
        "unknown_uuid": str(uuid.uuid4()),
        "unknown_code": "ZZZZ2222",
        "channel_off_uuid": str(seed.channel_off),
        "channel_off_code": await _invite_code(sessionmaker, seed.channel_off),
    }[reason]
    resp = await _add(client, login["account_token"], invite)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "clinic_invite_not_found"}
    assert await _count(sessionmaker, MessagePatient) == 0


def test_the_parser_never_guesses():
    assert parse_invite("0OIL-1234") is None, "look-alike symbols are not in the alphabet"
    assert parse_invite("A" * 600) is None
    assert parse_invite("https://portal.exemplo/clinicas/") is None
    assert normalize_invite_code("abcd 2345") == "ABCD2345"


# --- 5) Neither an e-mail match nor a separate old login puts a clinic in the account -----


async def test_a_clinic_that_only_shares_the_address_stays_out_until_invited(pclient):
    client, sessionmaker, seed = pclient
    seeded = await _seed_identity(sessionmaker, seed.only_secretaria)
    login = (await _email_login(client, sessionmaker)).json()
    assert login["clinics"] == [] and login["sibling_candidates"] == []
    renewed = (await _refresh(client)).json()
    assert renewed["clinics"] == []
    assert renewed["linked_sessions"] == [] and renewed["sibling_candidates"] == []

    # Invited, it enters with the handle it already had, and its consent is recorded once.
    added = await _add(client, login["account_token"], str(seed.only_secretaria))
    assert added.status_code == 200, added.text
    assert added.json()["patient_ref"] == str(seeded)
    consent = PatientConsentEvent.subject_ref == str(seeded)
    assert await _count(sessionmaker, PatientConsentEvent, consent) == 1


async def test_a_clinic_logged_into_separately_stays_out_until_its_own_session_returns(pclient):
    """The security review's case: before the account existed, the same address logged in at
    two clinics — a mother and a daughter sharing an inbox, say — and never linked them. Every
    identity from then carries a first-contact consent, so that consent cannot decide
    membership. Each old cookie reopens exactly what it reopened before; the other clinic
    joins only when ITS OWN session is used again. From then on the account is the address,
    and both browsers share it — the owner's "login só por e-mail", stated here on purpose."""
    client, sessionmaker, seed = pclient
    here, _ = await _seed_legacy_login(sessionmaker, seed.both, "cookie-aqui")
    there, _ = await _seed_legacy_login(sessionmaker, seed.only_secretaria, "cookie-la")

    body = (await _refresh(client, cookie="cookie-aqui")).json()
    assert [c["tenant_id"] for c in body["clinics"]] == [str(seed.both)]
    assert body["sibling_candidates"] == [] and body["patient_ref"] == str(here)
    client.cookies.clear()
    fresh = (await _email_login(client, sessionmaker)).json()
    assert [c["tenant_id"] for c in fresh["clinics"]] == [str(seed.both)], "a code adds nothing"

    client.cookies.clear()
    theirs = (await _refresh(client, cookie="cookie-la")).json()
    assert theirs["tenant_id"] == str(seed.only_secretaria)
    assert theirs["patient_ref"] == str(there)
    assert [c["tenant_id"] for c in theirs["clinics"]] == [
        str(seed.both),
        str(seed.only_secretaria),
    ]


async def test_a_confirmed_link_from_before_rides_the_first_refresh(pclient):
    """A login row and a link the previous deploy left: the old token keeps opening, the old
    cookie renews into the same two clinics, and the row joins the account with its id."""
    client, sessionmaker, seed = pclient
    login_ref, row_id = await _seed_legacy_login(sessionmaker, seed.both, "cookie-antigo")
    linked_ref, _ = await _seed_legacy_login(sessionmaker, seed.only_secretaria, linked=True)
    old_token = create_patient_token(
        tenant_id=str(seed.both),
        patient_ref=str(login_ref),
        session_id=str(row_id),
        login_session_id=str(row_id),
    )
    assert (await _threads(client, old_token)).status_code == 200

    resp = await _refresh(client, cookie="cookie-antigo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [c["tenant_id"] for c in body["clinics"]] == [str(seed.both), str(seed.only_secretaria)]
    assert {c["patient_ref"] for c in body["clinics"]} == {str(login_ref), str(linked_ref)}
    assert body["tenant_id"] == str(seed.both) and body["patient_ref"] == str(login_ref)
    assert decode_token(body["access_token"])["sid"] == str(row_id), "the login row kept its id"
    async with sessionmaker() as session:
        row = await session.get(MessagePatientSession, row_id)
        account = await session.scalar(select(MessagePatientAccount))
    assert row.account_id == account.id and row.patient_id == login_ref
    assert (await _threads(client, old_token)).status_code == 200


# --- The code under concurrency ----------------------------------------------------------


async def test_parallel_wrong_guesses_cannot_outrun_the_attempts_ceiling(pclient):
    """Both reviews' high finding: a burst of wrong guesses fired together gets at most the
    ceiling of comparisons, and the right code is dead after it. Incrementing a count read in
    Python let the burst overwrite itself (19 guesses left `attempts=1`)."""
    client, sessionmaker, seed = pclient
    await _request(client)
    code = await _peek_code(sessionmaker, None)
    wrong = "000000" if code != "000000" else "111111"
    ceiling = get_settings().PATIENT_OTP_MAX_ATTEMPTS

    burst = await asyncio.gather(*(_verify(client, wrong) for _ in range(ceiling * 3)))
    assert {r.status_code for r in burst} == {400}
    async with sessionmaker() as session:
        assert (await session.scalar(select(MessagePatientAccountOtp))).attempts == ceiling
    assert (await _verify(client, code)).status_code == 400


async def test_two_requests_with_the_right_code_open_the_account_once(pclient):
    client, sessionmaker, seed = pclient
    await _request(client)
    code = await _peek_code(sessionmaker, None)
    first, second = await asyncio.gather(_verify(client, code), _verify(client, code))
    assert sorted((first.status_code, second.status_code)) == [200, 400]
    assert await _count(sessionmaker, MessagePatientSession) == 1


async def test_an_account_created_mid_login_does_not_break_the_old_body(pclient, monkeypatch):
    """The FastAPI review's race: the portal of 2026-09-14 verifies with `tenant_id` while
    another request — an old cookie renewing — creates the same address's account. The insert
    that loses must not roll back and expire the clinic already loaded: that was a 500 with the
    code already burned."""
    client, sessionmaker, seed = pclient
    real_verify = patient_access.verify_account_otp

    async def _verify_then_a_concurrent_account(session, email, code):
        verified = await real_verify(session, email, code)
        async with sessionmaker() as other, other.begin():
            other.add(MessagePatientAccount(email=patient_access.normalize_email(email)))
        return verified

    monkeypatch.setattr(patient_access, "verify_account_otp", _verify_then_a_concurrent_account)
    resp = await _login(client, sessionmaker, seed.both)
    assert resp.status_code == 200, resp.text
    assert resp.json()["invited_tenant_id"] == str(seed.both)
    assert await _count(sessionmaker, MessagePatientAccount) == 1


# --- Tokens, budgets, logout -------------------------------------------------------------


async def test_each_token_opens_only_its_own_routes(pclient):
    client, sessionmaker, seed = pclient
    login = (await _login(client, sessionmaker, seed.both)).json()
    clinic_token = login["clinics"][0]["access_token"]
    assert (await _add(client, clinic_token, str(seed.only_secretaria))).status_code == 401
    assert (await _threads(client, login["account_token"])).status_code == 401
    staff_route = await client.get("/entitlements", headers=_bearer(login["account_token"]))
    assert staff_route.status_code == 401
    assert (
        await client.get("/entitlements/patient-invite", headers=_bearer(clinic_token))
    ).status_code == 401


async def test_an_invite_that_races_a_logout_dies_with_the_login(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    login = (await _email_login(client, sessionmaker)).json()
    real_add = patient_access.add_clinic

    async def _add_then_concurrent_logout(session, account, tenant):
        patient = await real_add(session, account, tenant)
        async with sessionmaker() as other:
            assert await patient_access.revoke_account_sessions(other, PATIENT_EMAIL) >= 1
        return patient

    monkeypatch.setattr(patient_access, "add_clinic", _add_then_concurrent_logout)
    resp = await _add(client, login["account_token"], str(seed.both))
    assert resp.status_code == 200, resp.text
    assert (await _threads(client, resp.json()["access_token"])).status_code == 401


async def test_the_invite_budget_is_per_account_and_spent_only_when_authenticated(
    pclient, monkeypatch
):
    from brain_api.api import patient_access as router_mod

    client, sessionmaker, seed = pclient
    login = (await _email_login(client, sessionmaker)).json()
    router_mod._link_limiter._hits.clear()
    monkeypatch.setattr(router_mod._link_limiter, "_limit_getter", lambda: 1)

    for _ in range(3):
        assert (await _add(client, "not-a-token", str(seed.both))).status_code == 401
    assert (await _add(client, login["account_token"], str(seed.both))).status_code == 200
    spoofed = await client.post(
        "/patient-access/clinics",
        headers={**_bearer(login["account_token"]), "X-Forwarded-For": "198.51.100.7"},
        json={"invite": str(seed.both)},
    )
    assert spoofed.status_code == 429


async def test_logout_with_the_account_token_ends_every_clinic(pclient):
    client, sessionmaker, seed = pclient
    login = (await _email_login(client, sessionmaker, invite=str(seed.both))).json()
    added = (await _add(client, login["account_token"], str(seed.only_secretaria))).json()

    assert (
        await client.post("/patient-access/logout", headers=_bearer(login["account_token"]))
    ).status_code == 200
    for token in (login["clinics"][0]["access_token"], added["access_token"]):
        assert (await _threads(client, token)).status_code == 401
    assert (await _add(client, login["account_token"], str(seed.both))).status_code == 401
    assert (await _refresh(client)).status_code == 401


# --- The transition contract -------------------------------------------------------------


async def test_the_transition_confirm_reopens_only_clinics_already_in_the_account(pclient):
    client, sessionmaker, seed = pclient
    login, sibling = await _two_clinic_account(client, sessionmaker, seed)
    assert login["sibling_candidates"] == [
        {
            "tenant_id": str(seed.only_secretaria),
            "clinic_name": CLINIC_ONLY_SECRETARIA,
            "already_linked": True,
        }
    ]
    sessions_before = await _count(sessionmaker, MessagePatientSession)
    events_before = await _count(sessionmaker, PatientConsentEvent)

    resp = await _confirm(client, login["access_token"], seed.only_secretaria)
    assert resp.status_code == 200, resp.text
    assert resp.json()["patient_ref"] == sibling["patient_ref"]
    assert await _count(sessionmaker, MessagePatientSession) == sessions_before
    assert await _count(sessionmaker, PatientConsentEvent) == events_before


async def test_every_clinic_rides_the_refresh_and_the_login_clinic_stays_on_top(pclient):
    client, sessionmaker, seed = pclient
    login, _ = await _two_clinic_account(client, sessionmaker, seed)
    linked = (await _confirm(client, login["access_token"], seed.only_secretaria)).json()

    body = (await _refresh(client)).json()
    assert [c["tenant_id"] for c in body["clinics"]] == [str(seed.both), str(seed.only_secretaria)]
    assert body["linked_sessions"] == body["clinics"]
    # The portal deployed on 2026-09-14 drops an account whose login clinic moves.
    assert body["tenant_id"] == str(seed.both)
    assert (await _threads(client, linked["access_token"])).status_code == 200
    for clinic in body["clinics"]:
        assert (await _threads(client, clinic["access_token"])).status_code == 200


async def test_an_email_only_login_keeps_the_clinic_it_was_pinned_to(pclient):
    """The FastAPI review's #8: naming "the first clinic by name" in the transition fields
    would move them when an earlier-sorting clinic is added — and the portal of 2026-09-14
    drops an account whose top-level clinic moves. The first clinic pins the login."""
    client, sessionmaker, seed = pclient
    login = (await _email_login(client, sessionmaker)).json()
    assert login["tenant_id"] is None
    await _add(client, login["account_token"], str(seed.only_secretaria))
    first = (await _refresh(client)).json()
    await _add(client, first["account_token"], str(seed.both))  # sorts BEFORE it by name
    second = (await _refresh(client)).json()
    assert [c["tenant_id"] for c in second["clinics"]] == [
        str(seed.both),
        str(seed.only_secretaria),
    ]
    assert first["tenant_id"] == second["tenant_id"] == str(seed.only_secretaria)
    assert first["patient_ref"] == second["patient_ref"]


# --- The clinic reads its own invite; logs ----------------------------------------------


async def test_the_staff_reads_its_own_invite(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    monkeypatch.setattr(get_settings(), "BRAIN_MESSAGE_PORTAL_URL", "https://portal.exemplo/")
    staff = create_access_token(sub=str(uuid.uuid4()), tenant_id=str(seed.both), role="doctor")
    async with sessionmaker() as session, session.begin():
        (await session.get(Tenant, seed.both)).patient_invite_code = None  # deploy-window tenant

    # Two reads racing on a tenant with no code must hand out ONE code — the stored one.
    first, second = await asyncio.gather(
        client.get("/entitlements/patient-invite", headers=_bearer(staff)),
        client.get("/entitlements/patient-invite", headers=_bearer(staff)),
    )
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    body = first.json()
    code = body["invite_code"]
    assert second.json()["invite_code"] == code
    assert await _invite_code(sessionmaker, seed.both) == code
    assert normalize_invite_code(code) == code
    assert body["tenant_id"] == str(seed.both) and body["brain_message_enabled"] is True
    assert body["invite_link"] == f"https://portal.exemplo/clinicas/?convite={code}"

    login = (await _email_login(client, sessionmaker, invite=body["invite_link"])).json()
    assert login["invited_tenant_id"] == str(seed.both)


async def test_account_logs_carry_no_address_no_clinic_name_no_account_id_no_handle(
    pclient, monkeypatch
):
    from brain_api.api import patient_access as router_mod

    events: list[tuple[str, dict]] = []

    class _Recorder:
        def info(self, event, **fields):
            events.append((event, fields))

        warning = error = debug = info

    monkeypatch.setattr(router_mod, "logger", _Recorder())
    monkeypatch.setattr(patient_access, "logger", _Recorder())

    client, sessionmaker, seed = pclient
    login = (await _email_login(client, sessionmaker, invite=str(seed.both))).json()
    await _add(client, login["account_token"], str(seed.only_secretaria))
    await _refresh(client)
    await client.post("/patient-access/logout", headers=_bearer(login["account_token"]))

    names = {name for name, _ in events}
    assert {
        "patient_clinic_added",
        "patient_session_issued",
        "patient_session_refreshed",
        "patient_account_sessions_revoked",
    } <= names
    added = [fields for name, fields in events if name == "patient_clinic_added"]
    assert all(set(fields) == {"tenant_id", "consent_events_recorded"} for fields in added)
    rendered = repr(events)
    account_id = decode_token(login["account_token"])["sub"]
    for personal in (PATIENT_EMAIL, CLINIC_BOTH, CLINIC_ONLY_SECRETARIA, account_id):
        assert personal not in rendered
