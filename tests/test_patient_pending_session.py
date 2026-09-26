"""The PENDING visit (2026-09-16): the patient talks — and books — before proving anything.

The owner reversed the order of the patient's first contact: link -> chat -> e-mail typed in
the conversation -> LGPD -> a REAL appointment -> code. Everything before that last step now
happens with nothing proven, which is a genuinely new state for this repo: until 0021 no
`MessagePatient` could exist without a verified address.

What each group here exists to catch, in the order the flow runs:

- **The handle must survive.** `MessagePatient.id` is secretarIA's `external_id` and PreCheck's
  `session_ref`; the appointment booked minutes before the code lives under it. A flow that
  minted a second identity at verification time would silently orphan that conversation, and
  nothing in the response would look wrong.
- **The address is claimed on the SERVICE leg, never by the browser.** If a pending token could
  name the inbox to be mailed, holding one would be enough to aim a code anywhere. The
  patient-facing request route therefore takes no address at all, and the tests check the
  refusal rather than trusting the absence of a field.
- **A pending token is not a session.** It must open no account route, and an account/clinic
  token must not pass for a pending one — in both directions, since all three are minted by the
  same signing key.
- **The collision the design cannot avoid**: a clinic that ALREADY has an identity for the
  proven address. Nothing is merged and no id is rewritten; the account keeps the old identity
  and the visit records the swap. Tested explicitly because it is the one branch where
  `patient_ref` legitimately changes.
- **No booking gate anywhere.** The owner closed "todos os produtos, sem gate": PreCheck must be
  reachable by direct link and by toggle with no appointment in sight.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import func, select

from brain_api.core.cookies import PATIENT_PENDING_COOKIE_NAME, PATIENT_SESSION_COOKIE_NAME
from brain_api.core.security import (
    PATIENT_PENDING_TOKEN_SCOPE,
    create_patient_pending_token,
    decode_token,
)
from brain_api.models import Tenant
from brain_api.models.patient_access import (
    CONSENT_KIND_CHANNEL_ACCESS,
    MessagePatient,
    MessagePatientAccount,
    MessagePatientAccountOtp,
    MessagePendingSession,
    PatientConsentEvent,
)
from brain_api.services.portal import patient_access
from tests.test_patient_access import (
    CLINIC_BOTH,
    PATIENT_EMAIL,
    _bearer,
    _configure_mesh,
    _peek_code,
    _seed_identity,
    _spy_transport,
)

_INTERNAL_KEY = "pending-pair-key"
_INVALID_CODE = "Código inválido ou expirado"
_OTHER_EMAIL = "outro@exemplo.com"


# --- helpers -----------------------------------------------------------------------------


def _internal_key(monkeypatch):
    """Turn the `/internal/*` gate on for this test (conftest forces the key empty)."""
    from brain_api.api import internal as internal_api

    monkeypatch.setattr(
        internal_api,
        "get_settings",
        lambda: SimpleNamespace(
            SECRETARIA_API_KEY=_INTERNAL_KEY,
            SECRETARIA_API_KEY_PREVIOUS="",
            PATIENT_OTP_EMAIL_RATE_LIMIT_PER_MIN=999,
            PATIENT_VERIFY_RATE_LIMIT_PER_MIN=999,
            PATIENT_OTP_EXPIRE_MINUTES=10,
        ),
    )
    return {"X-Internal-Api-Key": _INTERNAL_KEY}


async def _invite(sessionmaker, tenant_id) -> str:
    async with sessionmaker() as session:
        return (await session.get(Tenant, tenant_id)).patient_invite_code


async def _open(client, sessionmaker, tenant_id, product=None):
    """Open a pending visit through the clinic's short code, as a real link would."""
    body = {"invite": await _invite(sessionmaker, tenant_id)}
    if product is not None:
        body["product"] = product
    return await client.post("/patient-access/pending", json=body)


async def _claim(client, monkeypatch, tenant_id, patient_ref, email=PATIENT_EMAIL):
    """secretarIA's leg: the address the patient typed into the chat."""
    return await client.post(
        "/internal/brain-message/pending-email",
        headers=_internal_key(monkeypatch),
        json={
            "tenant_id": str(tenant_id),
            "external_id": str(patient_ref),
            "email": email,
        },
    )


async def _prove(client, sessionmaker, token, email=PATIENT_EMAIL):
    """Ask for the code and answer it — the last step of the owner's flow."""
    asked = await client.post("/patient-access/pending/request-otp", headers=_bearer(token))
    assert asked.status_code == 200, asked.text
    code = await _peek_code(sessionmaker, None, email)
    return await client.post(
        "/patient-access/pending/verify-otp", headers=_bearer(token), json={"code": code}
    )


async def _count(sessionmaker, model, *where) -> int:
    async with sessionmaker() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where))


async def _identity_call(client, monkeypatch, path, tenant_id, patient_ref, **extra):
    return await client.post(
        f"/internal/brain-message/{path}",
        headers=_internal_key(monkeypatch),
        json={
            "tenant_id": str(tenant_id),
            "external_id": str(patient_ref),
            **extra,
        },
    )


# --- 1) The handle is minted before the address, and never changes ------------------------


async def test_a_visitor_gets_an_identity_with_no_email_at_all(pclient):
    """The whole premise: an identity — and therefore a handle — before any address."""
    client, sessionmaker, seed = pclient
    resp = await _open(client, sessionmaker, seed.both)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["tenant_id"] == str(seed.both)
    assert body["clinic_name"] == CLINIC_BOTH
    assert body["email_claimed"] is False
    async with sessionmaker() as session:
        patient = await session.get(MessagePatient, uuid.UUID(body["patient_ref"]))
    assert patient is not None
    assert patient.email is None, "a pending identity must carry no address"
    assert patient.account_id is None, "and belong to no account"
    assert patient.tenant_id == seed.both


async def test_internal_inline_contract_promotes_only_on_the_browser_leg(
    pclient, monkeypatch
):
    """The wave-2/wave-3 seam: service verifies, browser receives the account.

    The pending bearer remains usable after the internal verification, because no service
    callback can set an HttpOnly cookie in the patient's browser. `/pending/complete` is the
    one-time exchange that returns the ordinary account contract and then kills that bearer.
    """
    from brain_api.services import secretaria_provisioning

    async def _queued(to, template, variables):
        assert template == "patient_access_otp"
        assert set(variables) == {"code", "ttl_minutes"}
        return True

    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _queued)
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    token = opened["pending_token"]
    handle = opened["patient_ref"]

    probe = await _identity_call(
        client, monkeypatch, "pending-identity", seed.both, handle
    )
    assert probe.status_code == 200
    assert probe.json() == {"status": "pending_unclaimed"}

    assert (await _claim(client, monkeypatch, seed.both, handle)).status_code == 200
    probe = await _identity_call(
        client, monkeypatch, "pending-identity", seed.both, handle
    )
    assert probe.json() == {"status": "pending_claimed"}

    requested = await _identity_call(
        client, monkeypatch, "pending-otp/request", seed.both, handle
    )
    assert requested.status_code == 200, requested.text
    # The masked address rides along since TASK-003 §3, so secretarIA's code card can name
    # the inbox. `PATIENT_EMAIL` is `paciente@exemplo.com`.
    assert requested.json() == {"status": "sent", "email_masked": "p***e@exemplo.com"}
    status_response = await client.get(
        "/patient-access/pending/status", headers=_bearer(token)
    )
    assert status_response.json() == {"state": "otp_sent"}

    wrong = await _identity_call(
        client,
        monkeypatch,
        "pending-otp/verify",
        seed.both,
        handle,
        code="000000",
    )
    assert wrong.status_code == 400
    assert (
        await client.get("/patient-access/pending/status", headers=_bearer(token))
    ).json() == {"state": "otp_sent"}

    # The composer hint is bounded by the real challenge TTL; a historical
    # request must not leave the Portal in code mode for the visit's full day.
    async with sessionmaker() as session:
        row = await session.scalar(
            select(MessagePendingSession).where(
                MessagePendingSession.patient_id == uuid.UUID(handle)
            )
        )
        row.otp_requested_at = datetime.now(UTC) - timedelta(minutes=11)
        await session.commit()
    assert (
        await client.get("/patient-access/pending/status", headers=_bearer(token))
    ).json() == {"state": "pending_claimed"}

    requested = await _identity_call(
        client, monkeypatch, "pending-otp/request", seed.both, handle
    )
    assert requested.status_code == 200

    code = await _peek_code(sessionmaker, None, PATIENT_EMAIL)
    verified = await _identity_call(
        client,
        monkeypatch,
        "pending-otp/verify",
        seed.both,
        handle,
        code=code,
    )
    assert verified.status_code == 200, verified.text
    # `patient_name` (2026-09-24): no account owns this address yet, so there is none.
    assert verified.json() == {"status": "verified", "patient_name": None}
    assert (
        await client.get("/patient-access/pending/status", headers=_bearer(token))
    ).json() == {"state": "verified"}

    # Still readable until the BROWSER receives its replacement credentials.
    before_exchange = await client.get(
        "/patient-access/threads", headers=_bearer(token)
    )
    assert before_exchange.status_code == 200

    completed = await client.post(
        "/patient-access/pending/complete", headers=_bearer(token)
    )
    assert completed.status_code == 200, completed.text
    account = completed.json()
    assert account["patient_ref"] == handle
    assert account["account_token"]
    assert "__Host-patient_session=" in completed.headers.get("set-cookie", "")

    assert (
        await client.get("/patient-access/pending/status", headers=_bearer(token))
    ).status_code == 401
    assert (
        await client.get("/patient-access/threads", headers=_bearer(token))
    ).status_code == 401

    probe = await _identity_call(
        client, monkeypatch, "pending-identity", seed.both, handle
    )
    assert probe.json() == {"status": "verified"}


async def test_pending_otp_cancel_drops_the_composer_out_of_code_mode(pclient, monkeypatch):
    """`identity_change_email`'s leg: the Portal input must not stay locked to 6 digits.

    Regression for a bug reported live (2026-09-25): the patient tapped "Mudar e-mail" on the
    Portal, the chat correctly asked for a new address, but the composer stayed in numeric
    OTP-only mode because `/pending/status` still said `otp_sent` from the OLD address's
    challenge. Cancelling clears exactly `otp_requested_at`, never the claimed address.
    """
    from brain_api.services import secretaria_provisioning

    async def _queued(to, template, variables):
        return True

    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _queued)
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    token = opened["pending_token"]
    handle = opened["patient_ref"]

    assert (await _claim(client, monkeypatch, seed.both, handle)).status_code == 200
    requested = await _identity_call(
        client, monkeypatch, "pending-otp/request", seed.both, handle
    )
    assert requested.status_code == 200, requested.text
    assert (
        await client.get("/patient-access/pending/status", headers=_bearer(token))
    ).json() == {"state": "otp_sent"}

    cancelled = await _identity_call(
        client, monkeypatch, "pending-otp/cancel", seed.both, handle
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json() == {"status": "cancelled"}
    # The composer unlocks (no longer `otp_sent`)...
    assert (
        await client.get("/patient-access/pending/status", headers=_bearer(token))
    ).json() == {"state": "pending_claimed"}
    # ...but the address itself is untouched — a re-claim still overwrites it, not this call.
    async with sessionmaker() as session:
        row = await session.scalar(
            select(MessagePendingSession).where(
                MessagePendingSession.patient_id == uuid.UUID(handle)
            )
        )
        assert row.email == PATIENT_EMAIL
        assert row.otp_requested_at is None

    # A second cancel, with nothing left to cancel, is a 200 and not an error.
    again = await _identity_call(
        client, monkeypatch, "pending-otp/cancel", seed.both, handle
    )
    assert again.status_code == 200
    assert again.json() == {"status": "nothing_to_cancel"}

    # An unknown handle is the same 404 every other route on this boundary uses.
    unknown = await _identity_call(
        client, monkeypatch, "pending-otp/cancel", seed.both, uuid.uuid4()
    )
    assert unknown.status_code == 404


async def test_internal_inline_contract_refuses_missing_email_and_cross_tenant_handle(
    pclient, monkeypatch
):
    """The service key is not authority to detach a visit from its tenant+handle pair."""
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()

    no_email = await _identity_call(
        client,
        monkeypatch,
        "pending-otp/request",
        seed.both,
        opened["patient_ref"],
    )
    assert no_email.status_code == 409

    wrong_tenant = await _identity_call(
        client,
        monkeypatch,
        "pending-otp/request",
        seed.only_secretaria,
        opened["patient_ref"],
    )
    assert wrong_tenant.status_code == 404

    strict = await client.post(
        "/internal/brain-message/pending-identity",
        headers=_internal_key(monkeypatch),
        json={
            "tenant_id": str(seed.both),
            "external_id": opened["patient_ref"],
            "email": PATIENT_EMAIL,
        },
    )
    assert strict.status_code == 422


async def test_internal_inline_request_never_promises_an_unqueued_code(
    pclient, monkeypatch
):
    """A notification refusal is 503 and leaves the Portal outside code mode."""
    from brain_api.services import secretaria_provisioning

    async def _refused(to, template, variables):
        return False

    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _refused)
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    assert (
        await _claim(client, monkeypatch, seed.both, opened["patient_ref"])
    ).status_code == 200

    requested = await _identity_call(
        client,
        monkeypatch,
        "pending-otp/request",
        seed.both,
        opened["patient_ref"],
    )
    assert requested.status_code == 503
    progress = await client.get(
        "/patient-access/pending/status",
        headers=_bearer(opened["pending_token"]),
    )
    assert progress.json() == {"state": "pending_claimed"}


async def test_the_handle_is_the_same_id_from_the_first_message_to_the_code(pclient, monkeypatch):
    """create pending -> claim e-mail -> verify code: `MessagePatient.id` never moves.

    This is the test the whole design exists for. secretarIA stores this id as `external_id`
    and PreCheck as `session_ref`; the appointment is booked BEFORE the code, so an id that
    changed at verification would leave that booking under a handle nobody looks up again.
    """
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    handle = opened["patient_ref"]

    claimed = await _claim(client, monkeypatch, seed.both, handle)
    assert claimed.status_code == 200, claimed.text
    # An address nobody has an account for yet, so both 2026-09-21 fields sit at their
    # defaults here; the branch that fills them has its own tests at the end of this file.
    assert claimed.json() == {
        "status": "claimed",
        "account_exists": False,
        "email_masked": None,
    }

    verified = await _prove(client, sessionmaker, opened["pending_token"])
    assert verified.status_code == 200, verified.text
    body = verified.json()

    assert body["patient_ref"] == handle, "the handle changed across the code"
    assert [c["patient_ref"] for c in body["clinics"]] == [handle]
    async with sessionmaker() as session:
        patient = await session.get(MessagePatient, uuid.UUID(handle))
        account = await session.scalar(
            select(MessagePatientAccount).where(MessagePatientAccount.email == PATIENT_EMAIL)
        )
    assert patient.email == PATIENT_EMAIL, "the address landed on the SAME row"
    assert patient.account_id == account.id
    # And exactly one identity exists at this clinic — no second row was minted.
    assert await _count(sessionmaker, MessagePatient, MessagePatient.tenant_id == seed.both) == 1


async def test_verifying_a_new_address_creates_the_account_with_this_clinic_inside(
    pclient, monkeypatch
):
    """An address nobody has used before: one account, one clinic, one consent event."""
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    await _claim(client, monkeypatch, seed.both, opened["patient_ref"])

    body = (await _prove(client, sessionmaker, opened["pending_token"])).json()

    assert body["invited_tenant_id"] == str(seed.both)
    assert [c["tenant_id"] for c in body["clinics"]] == [str(seed.both)]
    assert await _count(sessionmaker, MessagePatientAccount) == 1
    assert (
        await _count(
            sessionmaker,
            PatientConsentEvent,
            PatientConsentEvent.kind == CONSENT_KIND_CHANNEL_ACCESS,
            PatientConsentEvent.subject_ref == opened["patient_ref"],
        )
        == 1
    ), "the channel-access consent is recorded exactly once"
    # The session cookie of a real account login replaced the pending one.
    assert PATIENT_SESSION_COOKIE_NAME in client.cookies
    assert client.cookies.get(PATIENT_PENDING_COOKIE_NAME) in (None, "")


async def test_an_address_that_already_has_an_account_gains_this_clinic(pclient, monkeypatch):
    """The patient already talks to ANOTHER clinic: this one is incremented, nothing duplicated.

    The account model's own rule, reached through the new door: membership is a gesture, and
    opening this clinic's link and proving the address IS one. The handle of the conversation
    that just happened stays the account's identity here.
    """
    client, sessionmaker, seed = pclient
    # An account that already exists, with `only_secretaria` in it.
    async with sessionmaker() as session:
        account = await patient_access._ensure_account(session, PATIENT_EMAIL)
        other = await session.get(Tenant, seed.only_secretaria)
        first = await patient_access.add_clinic(session, account, other)
        first_id, account_id = first.id, account.id

    opened = (await _open(client, sessionmaker, seed.both)).json()
    handle = opened["patient_ref"]
    await _claim(client, monkeypatch, seed.both, handle)
    body = (await _prove(client, sessionmaker, opened["pending_token"])).json()

    assert body["patient_ref"] == handle, "the pending handle stayed this clinic's identity"
    assert {c["tenant_id"] for c in body["clinics"]} == {
        str(seed.both),
        str(seed.only_secretaria),
    }
    # The clinic the patient already had kept ITS id — nothing was merged or rewritten.
    assert {c["patient_ref"] for c in body["clinics"]} == {handle, str(first_id)}
    assert await _count(sessionmaker, MessagePatientAccount) == 1
    async with sessionmaker() as session:
        patient = await session.get(MessagePatient, uuid.UUID(handle))
    assert patient.account_id == account_id
    assert (
        await _count(
            sessionmaker,
            PatientConsentEvent,
            PatientConsentEvent.kind == CONSENT_KIND_CHANNEL_ACCESS,
            PatientConsentEvent.subject_ref == handle,
        )
        == 1
    ), "one consent event for the clinic that just entered the account"


async def test_a_clinic_that_already_knows_the_address_keeps_its_own_identity(
    pclient, monkeypatch
):
    """The one branch where `patient_ref` legitimately changes — and nothing is merged.

    This clinic already has an identity for the proven address (the same human talked to it
    before, from an account). That row owns the `(tenant_id, email)` slot AND its conversation
    history, so the pending handle cannot take it. Nothing is merged, no id is rewritten: the
    account uses the pre-existing identity and the visit records the swap in `superseded_by`,
    which is how the portal knows to switch threads.
    """
    client, sessionmaker, seed = pclient
    existing = await _seed_identity(sessionmaker, seed.both, PATIENT_EMAIL)

    opened = (await _open(client, sessionmaker, seed.both)).json()
    handle = uuid.UUID(opened["patient_ref"])
    await _claim(client, monkeypatch, seed.both, handle)
    body = (await _prove(client, sessionmaker, opened["pending_token"])).json()

    assert body["patient_ref"] == str(existing), "the account uses the clinic's older identity"
    assert body["patient_ref"] != str(handle)
    async with sessionmaker() as session:
        pending_identity = await session.get(MessagePatient, handle)
        visit = await session.scalar(
            select(MessagePendingSession).where(MessagePendingSession.patient_id == handle)
        )
    assert pending_identity is not None, "the conversation's identity is never deleted"
    assert pending_identity.email is None, "nor rewritten to take the slot"
    assert visit.superseded_by == existing
    assert visit.verified_at is not None and visit.revoked_at is not None


# --- 2) The address arrives on the service leg, never from the browser --------------------


async def test_the_code_cannot_be_asked_for_before_the_chat_captured_an_address(pclient):
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()

    resp = await client.post(
        "/patient-access/pending/request-otp", headers=_bearer(opened["pending_token"])
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "pending_email_missing"


async def test_the_browser_cannot_name_the_address_a_code_goes_to(pclient, monkeypatch):
    """The security property spelled as a test: neither patient-facing route reads an e-mail.

    If either did, a pending token — which proves only "this browser opened this chat" — would
    be enough to send a code to, and then verify, an inbox the visitor does not own. The two
    routes refuse in DIFFERENT ways, and both are checked here rather than assumed:

    - `request-otp` declares no body at all, so one that names an address is simply not read:
      the challenge that exists afterwards belongs to the address the CHAT captured, and no
      challenge exists for the one the caller asked for;
    - `verify-otp` declares a body with `extra="forbid"`, so an extra field is a loud 422.
    """
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    await _claim(client, monkeypatch, seed.both, opened["patient_ref"])
    headers = _bearer(opened["pending_token"])

    asked = await client.post(
        "/patient-access/pending/request-otp", headers=headers, json={"email": _OTHER_EMAIL}
    )
    assert asked.status_code == 200
    assert (
        await _count(
            sessionmaker,
            MessagePatientAccountOtp,
            MessagePatientAccountOtp.email == _OTHER_EMAIL,
        )
        == 0
    ), "a code was minted for an inbox the conversation never named"
    assert (
        await _count(
            sessionmaker,
            MessagePatientAccountOtp,
            MessagePatientAccountOtp.email == PATIENT_EMAIL,
        )
        == 1
    )

    code = await _peek_code(sessionmaker, None, PATIENT_EMAIL)
    assert (
        await client.post(
            "/patient-access/pending/verify-otp",
            headers=headers,
            json={"code": code, "email": _OTHER_EMAIL},
        )
    ).status_code == 422
    # The address the CHAT captured is the one that still works.
    assert (await _prove(client, sessionmaker, opened["pending_token"])).status_code == 200


async def test_the_claim_needs_the_internal_key_and_the_right_conversation(pclient, monkeypatch):
    """A clinic's key cannot move an address onto another clinic's conversation."""
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    handle = opened["patient_ref"]

    unauthenticated = await client.post(
        "/internal/brain-message/pending-email",
        json={"tenant_id": str(seed.both), "external_id": handle, "email": PATIENT_EMAIL},
    )
    assert unauthenticated.status_code in (401, 403)

    wrong_clinic = await _claim(client, monkeypatch, seed.only_secretaria, handle)
    assert wrong_clinic.status_code == 404
    unknown_handle = await _claim(client, monkeypatch, seed.both, uuid.uuid4())
    assert unknown_handle.status_code == 404
    assert wrong_clinic.json()["detail"] == unknown_handle.json()["detail"]

    async with sessionmaker() as session:
        visit = await session.scalar(select(MessagePendingSession))
    assert visit.email is None, "a refused claim wrote nothing"


async def test_a_corrected_typo_overwrites_the_claimed_address(pclient, monkeypatch):
    """People mistype e-mails in chats; the second answer is the one that counts."""
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    await _claim(client, monkeypatch, seed.both, opened["patient_ref"], email=_OTHER_EMAIL)
    await _claim(client, monkeypatch, seed.both, opened["patient_ref"], email=PATIENT_EMAIL)

    body = (await _prove(client, sessionmaker, opened["pending_token"])).json()
    async with sessionmaker() as session:
        account = await session.get(
            MessagePatientAccount, uuid.UUID(decode_token(body["account_token"])["sub"])
        )
    assert account.email == PATIENT_EMAIL


# --- 3) A pending token is not a session --------------------------------------------------


async def test_a_pending_token_carries_the_pending_scope_and_one_clinic(pclient):
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    claims = decode_token(opened["pending_token"])

    assert claims["scope"] == PATIENT_PENDING_TOKEN_SCOPE
    assert claims["tenant_id"] == str(seed.both)
    assert claims["sub"] == opened["patient_ref"]
    assert "email" not in claims


async def test_a_pending_token_opens_no_account_route(pclient):
    """It adds no clinic, ends no account, and reads no account body."""
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    headers = _bearer(opened["pending_token"])

    add = await client.post(
        "/patient-access/clinics",
        headers=headers,
        json={"invite": await _invite(sessionmaker, seed.only_secretaria)},
    )
    assert add.status_code == 401
    confirm = await client.post(
        f"/patient-access/siblings/{seed.only_secretaria}/confirm",
        headers=headers,
        json={"tenant_id": str(seed.only_secretaria)},
    )
    assert confirm.status_code == 401


async def test_an_account_token_is_not_a_pending_token(pclient, monkeypatch):
    """The other direction: the three populations cannot cross in either one."""
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    await _claim(client, monkeypatch, seed.both, opened["patient_ref"])
    account_body = (await _prove(client, sessionmaker, opened["pending_token"])).json()

    for token in (account_body["account_token"], account_body["clinics"][0]["access_token"]):
        resp = await client.post("/patient-access/pending/request-otp", headers=_bearer(token))
        assert resp.status_code == 401, resp.text


async def test_a_pending_token_dies_with_its_row(pclient, monkeypatch):
    """A visit that already became an account login stops answering, JWT intact or not."""
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    token = opened["pending_token"]
    await _claim(client, monkeypatch, seed.both, opened["patient_ref"])
    assert (await _prove(client, sessionmaker, token)).status_code == 200

    resp = await client.post("/patient-access/pending/request-otp", headers=_bearer(token))
    assert resp.status_code == 401


async def test_a_forged_pending_token_naming_another_clinic_is_refused(pclient):
    """The claims are checked AGAINST the row, never trusted — tenant isolation is structural."""
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    async with sessionmaker() as session:
        visit = await session.scalar(select(MessagePendingSession))

    forged = create_patient_pending_token(
        tenant_id=str(seed.only_secretaria),
        patient_ref=opened["patient_ref"],
        session_id=str(visit.id),
    )
    resp = await client.post("/patient-access/pending/request-otp", headers=_bearer(forged))
    assert resp.status_code == 401


# --- 4) The thread is reachable while pending ---------------------------------------------


async def test_a_pending_visitor_can_list_the_clinic_threads(pclient):
    """Without this the visitor has a handle and nowhere to talk."""
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()

    resp = await client.get("/patient-access/threads", headers=_bearer(opened["pending_token"]))
    assert resp.status_code == 200, resp.text
    assert [t["product"] for t in resp.json()["data"]] == ["secretaria", "precheck"]


async def test_the_relay_carries_the_pending_handle_to_the_product(pclient, monkeypatch):
    """The handle secretarIA receives as `external_id` IS the pending identity's id."""
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch)

    resp = await client.post(
        "/patient-access/threads/secretaria/messages",
        headers=_bearer(opened["pending_token"]),
        json={"text": "quero marcar uma consulta"},
    )
    assert resp.status_code == 200, resp.text
    assert calls[0]["json"]["external_id"] == opened["patient_ref"]
    assert calls[0]["json"]["tenant_id"] == str(seed.both)


# --- 5) Resuming, and the clinic's own gate -----------------------------------------------


async def test_a_reload_resumes_the_same_conversation(pclient):
    """The cookie is what keeps a refresh from starting a second conversation."""
    client, sessionmaker, seed = pclient
    first = (await _open(client, sessionmaker, seed.both)).json()
    second = (await _open(client, sessionmaker, seed.both)).json()

    assert second["patient_ref"] == first["patient_ref"]
    assert await _count(sessionmaker, MessagePendingSession) == 1
    assert await _count(sessionmaker, MessagePatient) == 1


async def test_a_clinic_with_the_channel_off_refuses_exactly_like_an_unknown_one(pclient):
    """`brain_message_enabled` is the door, and it leaks nothing about which clinics exist."""
    client, sessionmaker, seed = pclient
    async with sessionmaker() as session:
        closed = (await session.get(Tenant, seed.channel_off)).patient_invite_code

    off = await client.post("/patient-access/pending", json={"invite": closed})
    unknown = await client.post("/patient-access/pending", json={"invite": str(uuid.uuid4())})
    garbage = await client.post("/patient-access/pending", json={"invite": "not-a-code"})

    assert off.status_code == unknown.status_code == garbage.status_code == 404
    assert off.json()["detail"] == unknown.json()["detail"] == garbage.json()["detail"]
    assert await _count(sessionmaker, MessagePatient) == 0, "a refusal creates nothing"


async def test_a_clinic_that_closed_the_channel_mid_conversation_burns_no_code(
    pclient, monkeypatch
):
    """The gate is checked BEFORE the code is spent, exactly as the account login does it."""
    client, sessionmaker, seed = pclient
    opened = (await _open(client, sessionmaker, seed.both)).json()
    await _claim(client, monkeypatch, seed.both, opened["patient_ref"])
    await client.post(
        "/patient-access/pending/request-otp", headers=_bearer(opened["pending_token"])
    )
    code = await _peek_code(sessionmaker, None, PATIENT_EMAIL)
    async with sessionmaker() as session, session.begin():
        (await session.get(Tenant, seed.both)).brain_message_enabled = False

    resp = await client.post(
        "/patient-access/pending/verify-otp",
        headers=_bearer(opened["pending_token"]),
        json={"code": code},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == _INVALID_CODE
    async with sessionmaker() as session, session.begin():
        (await session.get(Tenant, seed.both)).brain_message_enabled = True
    # The code was never spent: it still works once the clinic is back.
    assert (
        await client.post(
            "/patient-access/pending/verify-otp",
            headers=_bearer(opened["pending_token"]),
            json={"code": code},
        )
    ).status_code == 200


async def test_the_unauthenticated_routes_share_one_tight_per_ip_budget(pclient, monkeypatch):
    """conftest zeroes this bucket for the rest of the module, so prove it here instead.

    It is the only thing standing between a script and a table full of anonymous identities:
    `POST /pending` WRITES a row per call, and there is no session to key a budget by. The
    lookup route shares the bucket deliberately — both are reachable with no credential, and
    splitting them would just double what one IP gets for free.
    """
    from brain_api.api.portal import patient_access as patient_api

    monkeypatch.setattr(patient_api._pending_limiter, "_limit_getter", lambda: 2)
    invite = await _invite(sessionmaker=pclient[1], tenant_id=pclient[2].both)
    client = pclient[0]

    first = await client.post("/patient-access/pending", json={"invite": invite})
    assert first.status_code == 200
    assert (
        await client.post("/patient-access/clinics/lookup", json={"invite": invite})
    ).status_code == 200
    third = await client.post("/patient-access/pending", json={"invite": invite})
    assert third.status_code == 429
    assert await _count(pclient[1], MessagePatient) == 1, "the refused call created nothing"


# --- 6) The direct PreCheck link, and the pre-login product list --------------------------


async def test_the_direct_precheck_link_opens_a_session_ref_with_no_email_and_no_account(pclient):
    """The owner's decision as a test: this link is OPEN — no e-mail, no code, no account.

    What brain-api owes PreCheck is a fresh `session_ref`; PreCheck's conductor creates the
    session itself on the first inbound turn (`resolve_session` is idempotent by ref), so
    nothing is pre-created upstream here.
    """
    client, sessionmaker, seed = pclient
    resp = await _open(client, sessionmaker, seed.both, product="precheck")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert uuid.UUID(body["patient_ref"])  # a usable session_ref, minted with nothing proven
    assert body["products"] == {"secretaria": True, "precheck": True}
    async with sessionmaker() as session:
        patient = await session.get(MessagePatient, uuid.UUID(body["patient_ref"]))
    assert patient.email is None and patient.account_id is None
    assert await _count(sessionmaker, MessagePatientAccount) == 0

    threads = await client.get("/patient-access/threads", headers=_bearer(body["pending_token"]))
    assert "precheck" in [t["product"] for t in threads.json()["data"]]


async def test_a_direct_precheck_link_to_a_clinic_without_precheck_is_refused_at_the_door(
    pclient,
):
    """Better than failing at the first message with an opaque 403 from the relay."""
    client, sessionmaker, seed = pclient
    resp = await _open(client, sessionmaker, seed.only_secretaria, product="precheck")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "product_unavailable"


async def test_the_pre_login_lookup_returns_only_the_product_booleans(pclient):
    """Everything a visitor may know about a clinic before logging in, and nothing more."""
    client, sessionmaker, seed = pclient
    resp = await client.post(
        "/patient-access/clinics/lookup",
        json={"invite": await _invite(sessionmaker, seed.only_secretaria)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert set(body) == {"tenant_id", "clinic_name", "products"}
    assert body["products"] == {"secretaria": True, "precheck": False}
    for leaked in ("plan", "status", "limits", "usage", "addons", "secretaria_tier"):
        assert leaked not in body


async def test_the_pre_login_lookup_hides_a_clinic_with_the_channel_off(pclient):
    client, sessionmaker, seed = pclient
    async with sessionmaker() as session:
        closed = (await session.get(Tenant, seed.channel_off)).patient_invite_code

    off = await client.post("/patient-access/clinics/lookup", json={"invite": closed})
    unknown = await client.post(
        "/patient-access/clinics/lookup", json={"invite": str(uuid.uuid4())}
    )
    assert off.status_code == unknown.status_code == 404
    assert off.json()["detail"] == unknown.json()["detail"]


# --- 7) No booking gate, anywhere ---------------------------------------------------------


async def test_precheck_is_reachable_with_no_appointment_anywhere_in_sight(pclient, monkeypatch):
    """"Todos os produtos, sem gate" (owner, 2026-09-16), proved on all three surfaces.

    The rule "PreCheck opens after a confirmed booking" survives only as the trigger for
    OPENING it automatically after secretarIA confirms one. It is not, and must not become, a
    condition of access: a visitor who has booked nothing reaches PreCheck by direct link, a
    pending visitor sees it in the toggle, and an account holder sees it in `/threads`.
    """
    client, sessionmaker, seed = pclient

    # (a) pre-login: the toggle offers it.
    lookup = await client.post(
        "/patient-access/clinics/lookup",
        json={"invite": await _invite(sessionmaker, seed.both)},
    )
    assert lookup.json()["products"]["precheck"] is True

    # (b) pending: the thread is listed AND the relay goes out.
    opened = (await _open(client, sessionmaker, seed.both, product="precheck")).json()
    listed = await client.get(
        "/patient-access/threads", headers=_bearer(opened["pending_token"])
    )
    assert "precheck" in [t["product"] for t in listed.json()["data"]]
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, payload={"state": "asking", "messages": []})
    relayed = await client.post(
        "/patient-access/threads/precheck/messages",
        headers=_bearer(opened["pending_token"]),
        json={"text": "oi"},
    )
    assert relayed.status_code == 200, relayed.text
    assert calls[0]["json"]["session_ref"] == opened["patient_ref"]

    # (c) with an account: still listed, still with no booking.
    await _claim(client, monkeypatch, seed.both, opened["patient_ref"])
    account_body = (await _prove(client, sessionmaker, opened["pending_token"])).json()
    after = await client.get(
        "/patient-access/threads",
        headers=_bearer(account_body["clinics"][0]["access_token"]),
    )
    assert "precheck" in [t["product"] for t in after.json()["data"]]


# --- 9) The masked address (TASK-003 §3) ---------------------------------------------------
#
# secretarIA renders "digite o código enviado para a***a@gmail.com", but it holds no copy of
# the address — the claim lives on the service leg precisely so the browser never names an
# inbox. So the mask has to come from here, and the RAW value must not come with it.


async def _request_code_internally(client, monkeypatch, tenant_id, handle):
    from brain_api.services import secretaria_provisioning

    async def _queued(to, template, variables):
        return True

    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _queued)
    return await _identity_call(client, monkeypatch, "pending-otp/request", tenant_id, handle)


async def test_internal_otp_request_returns_the_masked_address(pclient, monkeypatch):
    """The field secretarIA reads, on the route it actually calls."""
    client, sessionmaker, seed = pclient
    handle = (await _open(client, sessionmaker, seed.both)).json()["patient_ref"]
    assert (await _claim(client, monkeypatch, seed.both, handle)).status_code == 200

    resp = await _request_code_internally(client, monkeypatch, seed.both, handle)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "sent", "email_masked": "p***e@exemplo.com"}


async def test_the_raw_address_never_leaves_on_this_route(pclient, monkeypatch):
    """The property, not the shape: the full address appears in NO part of the response.

    Checked against the whole raw body rather than one field, so a future `email` or `to`
    added anywhere in the payload fails here instead of shipping.
    """
    client, sessionmaker, seed = pclient
    handle = (await _open(client, sessionmaker, seed.both)).json()["patient_ref"]
    assert (await _claim(client, monkeypatch, seed.both, handle)).status_code == 200

    resp = await _request_code_internally(client, monkeypatch, seed.both, handle)
    assert PATIENT_EMAIL not in resp.text
    # Nor the local part on its own — the half that identifies a person.
    assert PATIENT_EMAIL.split("@")[0] not in resp.text


async def test_the_raw_address_never_reaches_a_log_line_on_this_route(pclient, monkeypatch):
    """structlog's `PrintLoggerFactory` bypasses stdlib logging, so `caplog` is blind to these
    lines — record what the loggers are CALLED with instead (same technique as
    `test_precheck_handoff.py::test_context_never_reaches_a_log_line`)."""
    from brain_api.api.portal import internal as internal_api

    client, sessionmaker, seed = pclient
    handle = (await _open(client, sessionmaker, seed.both)).json()["patient_ref"]
    assert (await _claim(client, monkeypatch, seed.both, handle)).status_code == 200

    logged: list[tuple] = []

    def _record(*args: object, **kwargs: object) -> None:
        logged.append((args, kwargs))

    for level in ("debug", "info", "warning", "error"):
        monkeypatch.setattr(internal_api.logger, level, _record, raising=False)

    resp = await _request_code_internally(client, monkeypatch, seed.both, handle)
    assert resp.status_code == 200, resp.text

    # Non-vacuous: this path really did log before we assert on what is absent.
    assert any("patient_pending_otp_requested_internal" in repr(c) for c in logged), logged
    assert PATIENT_EMAIL not in repr(logged)
    # The MASK must not be logged either: a log line is not a patient-facing notice, and the
    # domain plus two characters is still more than `tenant_id` needs to be useful.
    assert "p***e" not in repr(logged)


async def test_a_visit_with_no_address_still_409s_rather_than_masking_nothing(
    pclient, monkeypatch
):
    """The field is non-optional on a 200, and this is why that is honest: a visit with no
    captured address never reaches a 200 at all."""
    client, sessionmaker, seed = pclient
    handle = (await _open(client, sessionmaker, seed.both)).json()["patient_ref"]

    resp = await _request_code_internally(client, monkeypatch, seed.both, handle)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "pending_email_missing"


# --- The address that already belongs to an account (2026-09-21) --------------------------
#
# The owner's ask: a visitor who types an address the platform already knows must be told so
# and sent straight to the code — "seu e-mail ja esta no nosso sistema, digite o codigo que
# mandamos para p***e@exemplo.com" — instead of being asked their name like a newcomer. The
# claim could not tell the two apart before, so secretarIA had no way to choose the question.


async def test_a_first_time_address_is_claimed_without_naming_an_account(pclient, monkeypatch):
    """Today's answer, unchanged. Nobody is being recognised, so there is nothing to describe,
    and the mask is `None` rather than `***`: absent, not redacted."""
    client, sessionmaker, seed = pclient
    handle = (await _open(client, sessionmaker, seed.both)).json()["patient_ref"]

    resp = await _claim(client, monkeypatch, seed.both, handle)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "claimed", "account_exists": False, "email_masked": None}


async def test_an_address_that_already_has_an_account_comes_back_masked(pclient, monkeypatch):
    """The branch the owner asked for, on the route secretarIA actually calls."""
    client, sessionmaker, seed = pclient
    async with sessionmaker() as session:
        await patient_access.open_account(session, PATIENT_EMAIL)
    handle = (await _open(client, sessionmaker, seed.both)).json()["patient_ref"]

    resp = await _claim(client, monkeypatch, seed.both, handle)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "status": "claimed",
        "account_exists": True,
        "email_masked": "p***e@exemplo.com",
    }

    # ONE inbox, ONE spelling: the notice that asks for the code must not show a second mask
    # of the same address. Both sides mask the normalized value, and this proves it stays so.
    asked = await _request_code_internally(client, monkeypatch, seed.both, handle)
    assert asked.json()["email_masked"] == resp.json()["email_masked"]


async def test_a_known_address_is_still_claimed_and_no_account_is_created_or_linked(
    pclient, monkeypatch
):
    """Detection READS. The visit records the address exactly as before — `issue_pending_otp`
    mails the code off `pending.email`, so a branch that skipped the write would break the code
    for precisely the returning patients it exists to serve — and no second account appears:
    an account is still only ever opened behind a proven code."""
    client, sessionmaker, seed = pclient
    async with sessionmaker() as session:
        await patient_access.open_account(session, PATIENT_EMAIL)
    handle = (await _open(client, sessionmaker, seed.both)).json()["patient_ref"]

    assert (await _claim(client, monkeypatch, seed.both, handle)).status_code == 200

    async with sessionmaker() as session:
        visit = await session.scalar(select(MessagePendingSession))
    assert visit.email == PATIENT_EMAIL
    assert visit.claimed_at is not None
    assert await _count(sessionmaker, MessagePatientAccount) == 1


async def test_the_claim_leaks_neither_the_raw_address_nor_the_mask_into_a_log(
    pclient, monkeypatch
):
    """The property, not the shape, on the claim route — same technique as the OTP route's
    twin above (structlog bypasses stdlib logging, so record the CALLS).

    `account_exists` is allowed in a log line: it names a clinic's fact, not an inbox. The
    mask is not — a log is nobody's notice, and two characters plus a domain is more than
    `tenant_id` needs to be useful.
    """
    from brain_api.api.portal import internal as internal_api

    client, sessionmaker, seed = pclient
    async with sessionmaker() as session:
        await patient_access.open_account(session, PATIENT_EMAIL)
    handle = (await _open(client, sessionmaker, seed.both)).json()["patient_ref"]

    logged: list[tuple] = []

    def _record(*args: object, **kwargs: object) -> None:
        logged.append((args, kwargs))

    for level in ("debug", "info", "warning", "error"):
        monkeypatch.setattr(internal_api.logger, level, _record, raising=False)

    resp = await _claim(client, monkeypatch, seed.both, handle)
    assert resp.status_code == 200, resp.text

    assert PATIENT_EMAIL not in resp.text
    # Nor the local part on its own — the half that identifies a person.
    assert PATIENT_EMAIL.split("@")[0] not in resp.text

    # Non-vacuous: this path really did log before we assert on what is absent.
    assert any("pending_email_claimed" in repr(c) for c in logged), logged
    assert PATIENT_EMAIL not in repr(logged)
    assert "p***e" not in repr(logged)
