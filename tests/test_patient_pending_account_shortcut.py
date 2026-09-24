"""A browser that already carries a live ACCOUNT opens a clinic's link (2026-09-24).

The symptom that motivated this, live on 2026-09-23: a patient with a verified account at one
clinic opened another clinic's link and was answered "📧 Seu e-mail já está no nosso sistema!
Para confirmar que é você, enviei um código de 6 dígitos" — because `POST /pending` never read
`__Host-patient_session`, minted an anonymous visit, and secretarIA's identity probe then said
`pending_unclaimed` for it. The owner's decision: with a live account in the browser, no e-mail,
no code, no confirmation screen — the conversation starts and the clinic is saved to the account.

What each group here exists to catch:

- **The symptom itself**, closed at the endpoint: with the account cookie the answer carries no
  visit, no pending token and no e-mail step, and the identity secretarIA will probe answers
  `verified` — the probe value that sends secretarIA straight to its menu.
- **Idempotence**: a reload, or a clinic the account already has, reuses the same handle and
  records no second consent event.
- **Nothing changes without a usable account**: no cookie, a dead one (revoked, expired,
  unknown, pre-account), or a live one without `X-Brain-Client` all get today's visit.
- **One person's cookie never reaches another person**: the row decides the account, and the
  body has no field to name one.

Docs: docs/CHECKPOINT_portal_sessao_ativa_pula_pendente.md.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from brain_api.api.portal import patient_access as patient_access_api
from brain_api.core.cookies import (
    CLIENT_HEADER_NAME,
    CLIENT_HEADER_VALUE,
    PATIENT_PENDING_COOKIE_NAME,
    PATIENT_SESSION_COOKIE_NAME,
)
from brain_api.core.security import PATIENT_TOKEN_SCOPE, decode_token
from brain_api.models import Tenant
from brain_api.models.patient_access import (
    CONSENT_KIND_CHANNEL_ACCESS,
    MessagePatient,
    MessagePatientAccount,
    MessagePatientSession,
    MessagePendingSession,
    PatientConsentEvent,
)
from brain_api.services.portal import patient_access
from tests.test_patient_access import (
    PATIENT_EMAIL,
    _bearer,
    _configure_mesh,
    _login,
    _peek_code,
    _spy_transport,
)
from tests.test_patient_pending_session import _identity_call

_WEB = {CLIENT_HEADER_NAME: CLIENT_HEADER_VALUE}
_OTHER_EMAIL = "outra.pessoa@exemplo.com"
_OPEN_PATH = "/internal/brain-message/open"


# --- helpers -----------------------------------------------------------------------------


async def _invite(sessionmaker, tenant_id) -> str:
    async with sessionmaker() as session:
        return (await session.get(Tenant, tenant_id)).patient_invite_code


async def _open(client, sessionmaker, tenant_id, *, headers=None, product=None):
    body = {"invite": await _invite(sessionmaker, tenant_id)}
    if product is not None:
        body["product"] = product
    return await client.post("/patient-access/pending", json=body, headers=headers or {})


async def _count(sessionmaker, model, *where) -> int:
    async with sessionmaker() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where))


async def _logged_in_at(client, sessionmaker, tenant_id, email=PATIENT_EMAIL) -> dict:
    """A real code login through `tenant_id` — it leaves the account cookie in the jar."""
    resp = await _login(client, sessionmaker, tenant_id, email)
    assert resp.status_code == 200, resp.text
    assert client.cookies.get(PATIENT_SESSION_COOKIE_NAME), "the login set no account cookie"
    return resp.json()


async def _channel_consents(sessionmaker, patient_ref) -> int:
    return await _count(
        sessionmaker,
        PatientConsentEvent,
        PatientConsentEvent.kind == CONSENT_KIND_CHANNEL_ACCESS,
        PatientConsentEvent.subject_ref == str(patient_ref),
    )


def _spy_add_clinic(monkeypatch) -> list:
    """Count `add_clinic` calls while keeping the real one."""
    calls: list = []
    real = patient_access.add_clinic

    async def _spy(session, account, tenant):
        calls.append(tenant.id)
        return await real(session, account, tenant)

    monkeypatch.setattr(patient_access, "add_clinic", _spy)
    return calls


# --- 1) The symptom: a live account never sees the e-mail step ------------------------------


async def test_a_live_account_opening_a_new_clinic_gets_no_visit_and_no_email_step(
    pclient, monkeypatch
):
    """The 2026-09-23 symptom, closed at the endpoint.

    Account verified at clinic A, link of clinic B opened in the same browser. Before: a visit,
    a `pending_token`, and secretarIA's probe answering `pending_unclaimed` (hence "Seu e-mail
    já está no nosso sistema!" once the address was typed). Now: no visit, no pending field,
    a clinic token that works, and the probe answering `verified`.
    """
    client, sessionmaker, seed = pclient
    login = await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    add_calls = _spy_add_clinic(monkeypatch)

    resp = await _open(client, sessionmaker, seed.both, headers=_WEB)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The contract part 2/3 consume.
    assert body["session_kind"] == "account"
    assert body["pending_token"] is None
    assert body["access_token"]
    assert body["email_claimed"] is False
    assert body["tenant_id"] == str(seed.both)
    assert body["products"] == {"secretaria": True, "precheck": True}
    # No visit anywhere: no row, and no pending cookie written to the browser.
    assert await _count(sessionmaker, MessagePendingSession) == 0
    assert PATIENT_PENDING_COOKIE_NAME not in resp.headers.get("set-cookie", "")
    assert add_calls == [seed.both], "add_clinic must run exactly once, for the invited clinic"

    # The identity is the ACCOUNT's, with the proven address — not an anonymous one.
    handle = uuid.UUID(body["patient_ref"])
    async with sessionmaker() as session:
        patient = await session.get(MessagePatient, handle)
        account = await session.scalar(
            select(MessagePatientAccount).where(MessagePatientAccount.email == PATIENT_EMAIL)
        )
    assert patient.tenant_id == seed.both
    assert patient.email == PATIENT_EMAIL
    assert patient.account_id == account.id
    assert await _channel_consents(sessionmaker, handle) == 1

    # The token is an ordinary clinic token of THIS login, and it opens this clinic's threads.
    claims = decode_token(body["access_token"])
    assert claims["scope"] == PATIENT_TOKEN_SCOPE
    assert claims["sub"] == str(handle)
    assert claims["tenant_id"] == str(seed.both)
    assert claims["sid"] == claims["login_sid"] == decode_token(login["account_token"])["sid"]
    threads = await client.get("/patient-access/threads", headers=_bearer(body["access_token"]))
    assert threads.status_code == 200, threads.text
    assert {t["product"] for t in threads.json()["data"]} == {"secretaria", "precheck"}

    # What secretarIA asks before its first turn. `verified` is the branch that goes straight
    # to the menu; `pending_unclaimed` is the one that asked for the e-mail on 2026-09-23.
    probe = await _identity_call(client, monkeypatch, "pending-identity", seed.both, handle)
    assert probe.status_code == 200, probe.text
    assert probe.json() == {"status": "verified"}

    # And the clinic is saved to the account: the next renewal lists it.
    renewed = await client.post("/patient-access/refresh", headers=_WEB)
    assert renewed.status_code == 200, renewed.text
    assert {c["tenant_id"] for c in renewed.json()["clinics"]} == {
        str(seed.both),
        str(seed.only_secretaria),
    }


async def test_the_account_branch_asks_secretaria_to_greet_the_account_handle(pclient, monkeypatch):
    """The same greeting trigger `POST /clinics` fires — under the ACCOUNT's handle.

    No new field on the wire: secretarIA learns this is a known person from its own probe
    (above). What matters here is that the open goes out, once, naming the handle the browser
    was just handed — a greeting under any other id would land in a thread nobody reads.
    """
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    resp = await _open(client, sessionmaker, seed.both, headers=_WEB)
    assert resp.status_code == 200, resp.text

    opens = [c for c in calls if c["url"].endswith(_OPEN_PATH)]
    assert len(opens) == 1
    assert opens[0]["base_url"].startswith("http://secretaria:8000")
    assert opens[0]["json"] == {
        "tenant_id": str(seed.both),
        "external_id": resp.json()["patient_ref"],
        "patient_name": None,
    }


async def test_a_precheck_link_with_a_live_account_opens_precheck_not_secretaria(
    pclient, monkeypatch
):
    """`produto=precheck` keeps meaning PreCheck on the account branch too."""
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    resp = await _open(client, sessionmaker, seed.both, headers=_WEB, product="precheck")
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_kind"] == "account"

    opens = [c for c in calls if c["url"].endswith(_OPEN_PATH)]
    assert [c["base_url"].split("/")[2] for c in opens] == ["precheck:8000"]


async def test_a_product_the_clinic_lacks_is_refused_before_the_account_is_touched(pclient):
    """`require_product` still runs first: a dead link adds nothing to anybody's account."""
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.both)
    before = await _count(sessionmaker, MessagePatient)

    resp = await _open(client, sessionmaker, seed.only_secretaria, headers=_WEB, product="precheck")
    assert resp.status_code == 403, resp.text
    assert await _count(sessionmaker, MessagePatient) == before
    assert await _count(sessionmaker, MessagePendingSession) == 0


# --- 2) Idempotence --------------------------------------------------------------------------


async def test_reopening_a_clinic_the_account_already_has_duplicates_nothing(pclient):
    """F5 on the new clinic, and the link of the clinic the login came through.

    Both must answer with the SAME handle the account already holds there: a second
    `MessagePatient` would be a second secretarIA conversation, and a second consent event a
    lie in the LGPD trail.
    """
    client, sessionmaker, seed = pclient
    login = await _logged_in_at(client, sessionmaker, seed.only_secretaria)

    first = (await _open(client, sessionmaker, seed.both, headers=_WEB)).json()
    patients, consents = (
        await _count(sessionmaker, MessagePatient),
        await _count(sessionmaker, PatientConsentEvent),
    )
    again = (await _open(client, sessionmaker, seed.both, headers=_WEB)).json()
    assert again["session_kind"] == "account"
    assert again["patient_ref"] == first["patient_ref"]

    # The clinic the account came in through: its existing identity, not a new one.
    home = (await _open(client, sessionmaker, seed.only_secretaria, headers=_WEB)).json()
    assert home["session_kind"] == "account"
    assert home["patient_ref"] == login["patient_ref"]

    assert await _count(sessionmaker, MessagePatient) == patients
    assert await _count(sessionmaker, PatientConsentEvent) == consents
    assert await _channel_consents(sessionmaker, first["patient_ref"]) == 1
    assert await _count(sessionmaker, MessagePendingSession) == 0


# --- 3) Without a usable account, today's visit — unchanged ----------------------------------


async def test_no_account_cookie_is_still_an_ordinary_visit(pclient):
    """The header alone is not an account: no cookie, same visit as before this change."""
    client, sessionmaker, seed = pclient
    resp = await _open(client, sessionmaker, seed.both, headers=_WEB)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_kind"] == "pending"
    assert body["pending_token"]
    assert body["access_token"] is None
    assert await _count(sessionmaker, MessagePendingSession) == 1


@pytest.mark.parametrize("headers", [{}, {CLIENT_HEADER_NAME: "mobile"}])
async def test_a_live_account_without_the_client_header_keeps_todays_visit(pclient, headers):
    """CSRF guard AND the deployed portal's safety net — absent, or present with a wrong value.

    The portal live before this change neither sends `X-Brain-Client` here nor understands an
    answer without `pending_token`. A cookie-authenticated write needs the header in this
    repo; without it the cookie is ignored, not refused, and that portal keeps working.
    """
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)

    resp = await _open(client, sessionmaker, seed.both, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_kind"] == "pending"
    assert body["pending_token"]
    assert await _count(sessionmaker, MessagePendingSession) == 1
    # Nothing was added to the account behind the missing header.
    async with sessionmaker() as session:
        account = await session.scalar(select(MessagePatientAccount))
        rows = await patient_access.account_clinics(session, account)
    assert {tenant.id for _, tenant in rows} == {seed.only_secretaria}


@pytest.mark.parametrize("death", ["revoked", "expired", "unknown", "pre_account"])
async def test_a_dead_account_cookie_and_no_pending_cookie_falls_back_to_a_new_visit(
    pclient, death
):
    """Revoked, expired, never issued, or a login from before the account model.

    Every one of them gets a brand-new visit, exactly as a browser with no cookie would — and
    in particular nothing is added to the account the dead cookie used to name.
    """
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    async with sessionmaker() as session, session.begin():
        if death == "revoked":
            await session.execute(
                update(MessagePatientSession).values(revoked_at=datetime.now(UTC))
            )
        elif death == "expired":
            await session.execute(
                update(MessagePatientSession).values(
                    expires_at=datetime.now(UTC) - timedelta(minutes=1)
                )
            )
        elif death == "pre_account":
            # The row a login wrote before 0020: bound to its identity, no account yet. It
            # joins one only when /refresh renews it — never here, by its address.
            await session.execute(update(MessagePatientSession).values(account_id=None))
    raw = client.cookies.get(PATIENT_SESSION_COOKIE_NAME)
    if death == "unknown":
        raw = "never-issued-value"
    assert not client.cookies.get(PATIENT_PENDING_COOKIE_NAME)
    # Sent explicitly, so the value under test is the only one the request can carry.
    client.cookies.clear()

    resp = await _open(
        client,
        sessionmaker,
        seed.both,
        headers={**_WEB, "Cookie": f"{PATIENT_SESSION_COOKIE_NAME}={raw}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_kind"] == "pending"
    assert body["pending_token"]
    assert body["access_token"] is None
    assert await _count(sessionmaker, MessagePendingSession) == 1
    assert await _count(sessionmaker, MessagePatient, MessagePatient.tenant_id == seed.both) == 1, (
        "only the anonymous visit's identity exists at the new clinic"
    )
    async with sessionmaker() as session:
        visitor = await session.get(MessagePatient, uuid.UUID(body["patient_ref"]))
    assert visitor.email is None and visitor.account_id is None


async def test_the_account_wins_over_a_pending_cookie_for_the_same_clinic(pclient):
    """A visit started before the login, then the same clinic's link again.

    The owner's rule is literal: logged in means no e-mail step. The visit is not resumed (it
    would ask for the address again); it is left alone, and the account's identity answers.
    """
    client, sessionmaker, seed = pclient
    visit = (await _open(client, sessionmaker, seed.both)).json()
    assert visit["session_kind"] == "pending"
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)

    pending_cookie = client.cookies.get(PATIENT_PENDING_COOKIE_NAME)
    assert pending_cookie

    resp = await _open(client, sessionmaker, seed.both, headers=_WEB)
    body = resp.json()
    assert body["session_kind"] == "account"
    assert body["patient_ref"] != visit["patient_ref"]
    # The visit's row is untouched — neither deleted nor rewritten — and so is its cookie:
    # a later logout in this browser gets back to that conversation.
    async with sessionmaker() as session:
        visitor = await session.get(MessagePatient, uuid.UUID(visit["patient_ref"]))
    assert visitor.email is None and visitor.account_id is None
    assert await _count(sessionmaker, MessagePendingSession) == 1
    assert "set-cookie" not in resp.headers
    assert client.cookies.get(PATIENT_PENDING_COOKIE_NAME) == pending_cookie


async def test_a_value_refresh_already_rotated_away_falls_back_to_a_visit(pclient):
    """The portal must let an in-flight renewal land before opening a link.

    `/refresh` rotates the cookie and accepts the replaced value for a short grace window;
    this route does NOT (it validates with `find_patient_session`, the current value only,
    and never rotates). Pinned here so a client that races the two sees a visit — the
    pre-change behaviour — rather than something undefined.
    """
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    replaced = client.cookies.get(PATIENT_SESSION_COOKIE_NAME)
    renewed = await client.post("/patient-access/refresh", headers=_WEB)
    assert renewed.status_code == 200, renewed.text
    client.cookies.clear()

    resp = await _open(
        client,
        sessionmaker,
        seed.both,
        headers={**_WEB, "Cookie": f"{PATIENT_SESSION_COOKIE_NAME}={replaced}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_kind"] == "pending"
    # And presenting it here did not count as reuse: the account is still alive.
    async with sessionmaker() as session:
        rows = (await session.scalars(select(MessagePatientSession))).all()
    assert rows and all(row.revoked_at is None for row in rows)


async def test_a_login_opened_by_email_alone_is_pinned_to_the_clinic_it_opens(pclient):
    """Like `POST /clinics`: the transition fields of `/refresh` stay stable from now on."""
    client, sessionmaker, seed = pclient
    asked = await client.post("/patient-access/request-otp", json={"email": PATIENT_EMAIL})
    assert asked.status_code == 200
    code = await _peek_code(sessionmaker, None, PATIENT_EMAIL)
    login = await client.post(
        "/patient-access/verify-otp", json={"email": PATIENT_EMAIL, "code": code}
    )
    assert login.status_code == 200, login.text
    async with sessionmaker() as session:
        row = await session.scalar(select(MessagePatientSession))
    assert row.patient_id is None, "a login by e-mail alone starts unpinned"

    body = (await _open(client, sessionmaker, seed.both, headers=_WEB)).json()
    assert body["session_kind"] == "account"
    async with sessionmaker() as session:
        row = await session.scalar(select(MessagePatientSession))
    assert (row.patient_id, row.tenant_id) == (uuid.UUID(body["patient_ref"]), seed.both)


async def test_the_account_budget_is_spent_only_once_the_cookie_is_proven(pclient, monkeypatch):
    """`_link_limiter`, keyed by the account: never touched for a dead cookie, 429 when spent."""
    client, sessionmaker, seed = pclient
    keys: list[str] = []

    class _Budget:
        allowed = True

        def allow(self, key):
            keys.append(key)
            return self.allowed

    budget = _Budget()
    monkeypatch.setattr(patient_access_api, "_link_limiter", budget)

    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    live = client.cookies.get(PATIENT_SESSION_COOKIE_NAME)
    client.cookies.clear()
    dead = await _open(
        client,
        sessionmaker,
        seed.both,
        headers={**_WEB, "Cookie": f"{PATIENT_SESSION_COOKIE_NAME}=never-issued"},
    )
    assert dead.json()["session_kind"] == "pending"
    assert keys == [], "an unauthenticated request must not spend any account's budget"

    budget.allowed = False
    client.cookies.clear()
    spent = await _open(
        client,
        sessionmaker,
        seed.only_secretaria,
        headers={**_WEB, "Cookie": f"{PATIENT_SESSION_COOKIE_NAME}={live}"},
    )
    assert spent.status_code == 429
    async with sessionmaker() as session:
        account = await session.scalar(select(MessagePatientAccount))
    assert keys == [str(account.id)]


# --- 4) One person's cookie never reaches another person -------------------------------------


async def test_one_accounts_cookie_never_opens_or_links_the_clinic_for_another_person(pclient):
    """Adversarial. Person B already talks to clinic `both`; person A's browser opens its link.

    A must get A's own new identity there. B's identity keeps B's address and B's account,
    B's account gains nothing, and the token handed to A names A's handle only.
    """
    client, sessionmaker, seed = pclient
    async with sessionmaker() as session:
        other = await patient_access._ensure_account(session, _OTHER_EMAIL)
        await session.commit()
        clinic = await session.get(Tenant, seed.both)
        b_identity = await patient_access.add_clinic(session, other, clinic)
        b_handle, b_account_id = b_identity.id, other.id

    await _logged_in_at(client, sessionmaker, seed.only_secretaria)  # person A
    resp = await _open(client, sessionmaker, seed.both, headers=_WEB)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["session_kind"] == "account"
    assert body["patient_ref"] != str(b_handle)
    assert decode_token(body["access_token"])["sub"] == body["patient_ref"]
    async with sessionmaker() as session:
        a_identity = await session.get(MessagePatient, uuid.UUID(body["patient_ref"]))
        b_after = await session.get(MessagePatient, b_handle)
        b_account = await session.get(MessagePatientAccount, b_account_id)
        b_clinics = await patient_access.account_clinics(session, b_account)
    assert a_identity.email == PATIENT_EMAIL
    assert a_identity.account_id != b_account_id
    assert (b_after.email, b_after.account_id) == (_OTHER_EMAIL, b_account_id)
    assert [tenant.id for _, tenant in b_clinics] == [seed.both]


@pytest.mark.parametrize(
    "extra",
    [
        {"email": _OTHER_EMAIL},
        {"account_id": str(uuid.uuid4())},
        {"patient_ref": str(uuid.uuid4())},
        {"tenant_id": str(uuid.uuid4())},
    ],
)
async def test_the_body_has_no_field_through_which_to_name_an_account(pclient, extra):
    """`extra="forbid"`: the account is the cookie's row's, never something the request names."""
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    before = await _count(sessionmaker, MessagePatient)

    resp = await client.post(
        "/patient-access/pending",
        json={"invite": await _invite(sessionmaker, seed.both), **extra},
        headers=_WEB,
    )
    assert resp.status_code == 422
    assert await _count(sessionmaker, MessagePatient) == before


async def test_the_account_branch_logs_the_clinic_and_nothing_that_joins_a_person(
    pclient, monkeypatch
):
    """Tenant id only: no address, no handle, no account id (cross-tenant-account-linking)."""
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    events: list[tuple[str, dict]] = []

    class _Recorder:
        def info(self, event, **kw):
            events.append((event, kw))

        warning = error = debug = info

    monkeypatch.setattr(patient_access_api, "logger", _Recorder())
    body = (await _open(client, sessionmaker, seed.both, headers=_WEB)).json()

    (line,) = [kw for event, kw in events if event == "patient_pending_skipped_for_account"]
    assert line == {"tenant_id": str(seed.both)}
    flat = repr(events)
    assert PATIENT_EMAIL not in flat
    assert body["patient_ref"] not in flat
