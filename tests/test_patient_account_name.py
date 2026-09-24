"""The patient's NAME follows the account to its next clinic (2026-09-24).

A live account added to a new clinic skips e-mail and code (see
test_patient_pending_account_shortcut.py), but the clinic still needs the name — the calendar
event's title, the professional's e-mail, the PreCheck hand-off (owner, 2026-09-24). secretarIA
asks it once and reports it (`POST /internal/brain-message/patient-name`); brain-api keeps it
on the clinic identity and hands it to the account's next clinic:

- on the `open` of a clinic the account is added to (`add_clinic` -> `patient_name`);
- on the internal code verification of a visit whose proven address owns an account
  (`PendingOtpVerifyOut.patient_name`).

What each group here exists to catch:

- **The route**: saved on exactly the identity both keys name, refused otherwise, strict body.
- **The symptom**: the next clinic's `open` carries the name, so secretarIA does not ask.
- **Only through the account**: another person's name, or an unlinked row sharing the address,
  never reaches anybody. Most recent TYPED name wins; a copied one never outranks it.
- **No PII in logs.**

Docs: docs/CHECKPOINT_portal_sessao_ativa_pula_pendente.md §8.
"""

import logging
import uuid

import pytest
from sqlalchemy import select

from brain_api.models.patient_access import MessagePatient
from brain_api.services.portal import patient_access
from tests.test_patient_access import PATIENT_EMAIL, _configure_mesh, _peek_code, _spy_transport
from tests.test_patient_pending_account_shortcut import _WEB, _logged_in_at, _open as _open_link
from tests.test_patient_pending_session import (
    _claim,
    _identity_call,
    _internal_key,
    _request_code_internally,
)

_OPEN_PATH = "/internal/brain-message/open"
_OTHER_EMAIL = "outra.pessoa@exemplo.com"


async def _handle_at(sessionmaker, tenant_id, email=PATIENT_EMAIL) -> uuid.UUID:
    async with sessionmaker() as session:
        return await session.scalar(
            select(MessagePatient.id).where(
                MessagePatient.tenant_id == tenant_id, MessagePatient.email == email
            )
        )


async def _name_of(sessionmaker, patient_id) -> str | None:
    async with sessionmaker() as session:
        return (await session.get(MessagePatient, patient_id)).name


async def _report(client, monkeypatch, tenant_id, handle, name="Maria Silva"):
    return await _identity_call(client, monkeypatch, "patient-name", tenant_id, handle, name=name)


def _opens(calls) -> list[dict]:
    return [c["json"] for c in calls if c["url"].endswith(_OPEN_PATH)]


# --- 1) The route --------------------------------------------------------------------------


async def test_the_name_is_saved_on_the_identity_both_keys_name(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    handle = await _handle_at(sessionmaker, seed.only_secretaria)

    resp = await _report(client, monkeypatch, seed.only_secretaria, handle, name="  Maria Silva ")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "saved"}
    assert await _name_of(sessionmaker, handle) == "Maria Silva"


async def test_another_clinics_key_cannot_name_this_clinics_patient(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    handle = await _handle_at(sessionmaker, seed.only_secretaria)

    wrong_clinic = await _report(client, monkeypatch, seed.both, handle)
    unknown = await _report(client, monkeypatch, seed.only_secretaria, uuid.uuid4())

    assert wrong_clinic.status_code == 404
    assert unknown.status_code == 404
    assert await _name_of(sessionmaker, handle) is None


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "x" * 256},
        {"name": "Maria", "email": PATIENT_EMAIL},
        {"name": "Maria", "account_id": str(uuid.uuid4())},
    ],
)
async def test_the_body_is_strict(pclient, monkeypatch, body):
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    handle = await _handle_at(sessionmaker, seed.only_secretaria)

    resp = await _identity_call(
        client, monkeypatch, "patient-name", seed.only_secretaria, handle, **body
    )

    assert resp.status_code == 422
    assert await _name_of(sessionmaker, handle) is None


async def test_the_route_is_behind_the_internal_key(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    handle = await _handle_at(sessionmaker, seed.only_secretaria)
    _internal_key(monkeypatch)

    resp = await client.post(
        "/internal/brain-message/patient-name",
        headers={"X-Internal-Api-Key": "wrong"},
        json={"tenant_id": str(seed.only_secretaria), "external_id": str(handle), "name": "Ana"},
    )

    assert resp.status_code == 401
    assert await _name_of(sessionmaker, handle) is None


# --- 2) The symptom: the next clinic already knows the name ---------------------------------


async def test_the_accounts_next_clinic_is_opened_with_its_name(pclient, monkeypatch):
    """Named at clinic A; clinic B's link with the account cookie -> `open` carries the name."""
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    handle_a = await _handle_at(sessionmaker, seed.only_secretaria)
    assert (await _report(client, monkeypatch, seed.only_secretaria, handle_a)).status_code == 200
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    resp = await _open_link(client, sessionmaker, seed.both, headers=_WEB)

    assert resp.status_code == 200, resp.text
    assert resp.json()["session_kind"] == "account"
    handle_b = resp.json()["patient_ref"]
    assert _opens(calls) == [
        {"tenant_id": str(seed.both), "external_id": handle_b, "patient_name": "Maria Silva"}
    ]
    # Kept on B's identity too, so a reload or B's own sessions do not depend on A.
    assert await _name_of(sessionmaker, uuid.UUID(handle_b)) == "Maria Silva"


async def test_an_account_that_never_gave_a_name_opens_with_none(pclient, monkeypatch):
    """Nothing invented: secretarIA gets `null` and asks once."""
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    resp = await _open_link(client, sessionmaker, seed.both, headers=_WEB)

    assert [o["patient_name"] for o in _opens(calls)] == [None]
    assert await _name_of(sessionmaker, uuid.UUID(resp.json()["patient_ref"])) is None


async def test_a_new_anonymous_visit_carries_no_name(pclient, monkeypatch):
    """Regression: without the account cookie the `open` is today's, name-free."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    resp = await _open_link(client, sessionmaker, seed.both)

    assert resp.json()["session_kind"] == "pending"
    assert [o["patient_name"] for o in _opens(calls)] == [None]


# --- 3) Only through the account ------------------------------------------------------------


async def test_the_latest_typed_name_wins_and_a_copy_never_outranks_it(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    handle_a = await _handle_at(sessionmaker, seed.only_secretaria)
    await _report(client, monkeypatch, seed.only_secretaria, handle_a, name="Maria")
    resp = await _open_link(client, sessionmaker, seed.both, headers=_WEB)
    handle_b = uuid.UUID(resp.json()["patient_ref"])
    assert await _name_of(sessionmaker, handle_b) == "Maria", "B did not get A's name"

    # The patient corrects it at B; the correction is what the account now answers.
    await _report(client, monkeypatch, seed.both, handle_b, name="Maria Silva")

    async with sessionmaker() as session:
        account_id = (await session.get(MessagePatient, handle_a)).account_id
        assert await patient_access.account_display_name(session, account_id) == "Maria Silva"


async def test_another_persons_name_never_crosses(pclient, monkeypatch):
    """Person B named at clinic X; person A's account opening X gets no name of B's."""
    client, sessionmaker, seed = pclient
    # B: an identity at `both`, with a name, in B's own account.
    await _logged_in_at(client, sessionmaker, seed.both, email=_OTHER_EMAIL)
    handle_b = await _handle_at(sessionmaker, seed.both, email=_OTHER_EMAIL)
    await _report(client, monkeypatch, seed.both, handle_b, name="Pessoa B")
    client.cookies.clear()

    # A: account at `only_secretaria`, no name, opens `both`.
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})
    resp = await _open_link(client, sessionmaker, seed.both, headers=_WEB)

    assert [o["patient_name"] for o in _opens(calls)] == [None]
    assert resp.json()["patient_ref"] != str(handle_b)
    assert await _name_of(sessionmaker, handle_b) == "Pessoa B"


async def test_an_unlinked_row_sharing_the_address_is_not_the_accounts(pclient, monkeypatch):
    """A named row with the same e-mail but NO account link is not read (attribute != link)."""
    client, sessionmaker, seed = pclient
    async with sessionmaker() as session:
        session.add(
            MessagePatient(
                tenant_id=seed.channel_off, email=PATIENT_EMAIL, account_id=None, name="Intrusa"
            )
        )
        await session.commit()
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    await _open_link(client, sessionmaker, seed.both, headers=_WEB)

    assert [o["patient_name"] for o in _opens(calls)] == [None]


# --- 4) The code path: a known address typed into a new clinic ------------------------------


async def _visit_proven(client, sessionmaker, monkeypatch, tenant_id, email=PATIENT_EMAIL):
    opened = await _open_link(client, sessionmaker, tenant_id)
    handle = opened.json()["patient_ref"]
    assert (await _claim(client, monkeypatch, tenant_id, handle, email)).status_code == 200
    requested = await _request_code_internally(client, monkeypatch, tenant_id, handle)
    assert requested.status_code == 200, requested.text
    code = await _peek_code(sessionmaker, None, email)
    return await _identity_call(
        client, monkeypatch, "pending-otp/verify", tenant_id, handle, code=code
    )


async def test_the_code_that_proves_a_known_address_also_brings_its_name(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    handle_a = await _handle_at(sessionmaker, seed.only_secretaria)
    await _report(client, monkeypatch, seed.only_secretaria, handle_a)
    client.cookies.clear()  # no account cookie: the anonymous-visit path of today

    verified = await _visit_proven(client, sessionmaker, monkeypatch, seed.both)

    assert verified.status_code == 200, verified.text
    assert verified.json() == {"status": "verified", "patient_name": "Maria Silva"}


async def test_a_wrong_code_brings_no_name(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    handle_a = await _handle_at(sessionmaker, seed.only_secretaria)
    await _report(client, monkeypatch, seed.only_secretaria, handle_a)
    client.cookies.clear()
    opened = await _open_link(client, sessionmaker, seed.both)
    handle = opened.json()["patient_ref"]
    await _claim(client, monkeypatch, seed.both, handle)
    await _request_code_internally(client, monkeypatch, seed.both, handle)

    wrong = await _identity_call(
        client, monkeypatch, "pending-otp/verify", seed.both, handle, code="000000"
    )

    assert wrong.status_code == 400
    assert "Maria" not in wrong.text


# --- 5) No PII in logs ----------------------------------------------------------------------


async def test_the_name_never_reaches_a_log(pclient, monkeypatch, caplog):
    client, sessionmaker, seed = pclient
    await _logged_in_at(client, sessionmaker, seed.only_secretaria)
    handle_a = await _handle_at(sessionmaker, seed.only_secretaria)
    _configure_mesh(monkeypatch)
    _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    with caplog.at_level(logging.DEBUG):
        await _report(client, monkeypatch, seed.only_secretaria, handle_a, name="Nome Secreto")
        await _open_link(client, sessionmaker, seed.both, headers=_WEB)

    # The application's own loggers only: the SQLite driver's DEBUG trace echoes bound
    # parameters of every statement, which is a test-harness artefact, not an app log.
    app_lines = [
        r.getMessage() for r in caplog.records if not r.name.startswith(("aiosqlite", "sqlalchemy"))
    ]
    assert app_lines, "nothing was captured — the assertion below would pass vacuously"
    assert all("Nome Secreto" not in line for line in app_lines)
