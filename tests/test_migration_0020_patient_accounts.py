"""Migration 0020 — and the Postgres-only statements behind it — on a REAL Postgres.

The rest of the suite builds its schema with `create_all` on SQLite, which proves nothing
about a migration, about `ON CONFLICT`, or about row locks. This module runs the real Alembic
chain against the DISPOSABLE Postgres named by `BRAIN_MIGRATION_PG_URL` (e.g. a throwaway
`postgres:16` container) and is skipped without it. It runs `alembic downgrade base`, so it
refuses any host but this machine.

What it pins (docs/CHECKPOINT_portal_clinicas_convite.md):
- no `MessagePatient.id` changes, through upgrade, downgrade and upgrade again;
- every seeded identity carries the first-contact consent the previous deploy wrote with each
  code, as in production — and the backfill still adopts ONLY identities with a confirmed link;
- an old cookie renews into exactly what it reopened before (its clinic + confirmed links),
  not a clinic the address logged into separately, nor a channel-off one;
- the conditional attempt spend, the burn, and the `ON CONFLICT` / `WHERE NOT EXISTS` inserts
  hold under real concurrency (separate connections);
- every tenant gets a unique, well-formed invite code.
"""

import asyncio
import hashlib
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from brain_api.config import get_settings
from brain_api.core.cookies import (
    CLIENT_HEADER_NAME,
    CLIENT_HEADER_VALUE,
    PATIENT_SESSION_COOKIE_NAME,
)
from brain_api.core.database import get_session
from brain_api.core.invite_codes import ALPHABET, CODE_LENGTH
from brain_api.core.security import decode_token
from brain_api.main import app
from brain_api.models import Tenant
from brain_api.models.patient_access import MessagePatientAccount
from brain_api.services import patient_access

PG_URL = os.environ.get("BRAIN_MIGRATION_PG_URL", "")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="BRAIN_MIGRATION_PG_URL not set (a DISPOSABLE Postgres only)"
)

REPO = Path(__file__).resolve().parents[1]
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
EMAIL = "conta.antiga@exemplo.com"
OTHER_EMAIL = "outra.conta@exemplo.com"
OLD_COOKIE = "cookie-emitido-antes-da-0020"
SEPARATE_COOKIE = "cookie-de-outro-aparelho"


def _guard() -> None:
    host = make_url(PG_URL).host
    if host not in _LOCAL_HOSTS:
        pytest.fail(f"refusing to run migrations against a non-local host: {host!r}")


def _alembic(*args: str) -> None:
    env = {**os.environ, "DATABASE_URL": PG_URL}
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=REPO, env=env, check=True)


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _rows(engine, sql: str, params: dict | None = None) -> list[tuple]:
    async with engine.connect() as conn:
        return [tuple(row) for row in (await conn.execute(text(sql), params or {})).all()]


async def _refresh_with(sessionmaker, cookie: str):
    async def _override_get_session():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            return await client.post(
                "/patient-access/refresh",
                headers={
                    CLIENT_HEADER_NAME: CLIENT_HEADER_VALUE,
                    "Cookie": f"{PATIENT_SESSION_COOKIE_NAME}={cookie}",
                },
            )
    finally:
        app.dependency_overrides.clear()


async def test_0020_keeps_every_handle_and_what_an_old_cookie_reopens():
    _guard()
    engine = create_async_engine(PG_URL, poolclass=NullPool)
    _alembic("downgrade", "base")
    _alembic("upgrade", "0019_patient_session_rotation")

    now = datetime.now(UTC)
    names = ("login", "linked", "separate", "dead", "off", "other")
    tenants = {name: uuid.uuid4() for name in names}
    patients = {name: uuid.uuid4() for name in names}
    emails = {name: (OTHER_EMAIL if name == "other" else EMAIL) for name in names}
    # (tenant, consent kinds) — channel access on every one, as each code wrote it.
    links = {"linked", "off"}
    sessions = {
        # name -> (session id, cookie value, lifetime)
        "login": (uuid.uuid4(), OLD_COOKIE, timedelta(days=30)),
        "linked": (uuid.uuid4(), "linha-da-clinica-vinculada", timedelta(minutes=30)),
        "separate": (uuid.uuid4(), SEPARATE_COOKIE, timedelta(days=30)),
        "dead": (uuid.uuid4(), "cookie-vencido", -timedelta(days=1)),
        "other": (uuid.uuid4(), "cookie-de-outra-conta", timedelta(days=30)),
    }

    async with engine.begin() as conn:
        for name in names:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, clinic_name, brain_message_enabled) "
                    "VALUES (:id, :name, :on)"
                ),
                {"id": tenants[name], "name": f"Clinica {name}", "on": name != "off"},
            )
            await conn.execute(
                text(
                    "INSERT INTO message_patients (id, tenant_id, email, created_at) "
                    "VALUES (:id, :tenant, :email, :at)"
                ),
                {"id": patients[name], "tenant": tenants[name], "email": emails[name], "at": now},
            )
            kinds = ["brain_message_channel_access"]
            if name in links:
                kinds.append("brain_message_account_link")
            for kind in kinds:
                await conn.execute(
                    text(
                        "INSERT INTO patient_consent_events (id, tenant_id, subject_ref, kind, "
                        "legal_basis) VALUES (:id, :tenant, :ref, :kind, 'TODO_LAWYER')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tenant": tenants[name],
                        "ref": str(patients[name]),
                        "kind": kind,
                    },
                )
        for name, (sid, raw, ttl) in sessions.items():
            await conn.execute(
                text(
                    "INSERT INTO message_patient_sessions (id, patient_id, tenant_id, token_hash, "
                    "expires_at, created_at) VALUES (:id, :pid, :tenant, :hash, :exp, :at)"
                ),
                {
                    "id": sid,
                    "pid": patients[name],
                    "tenant": tenants[name],
                    "hash": _sha(raw),
                    "exp": now + ttl,
                    "at": now,
                },
            )

    _alembic("upgrade", "head")

    ids = {row[0] for row in await _rows(engine, "SELECT id FROM message_patients")}
    assert ids == set(patients.values()), "a clinic handle changed"
    accounts = dict(await _rows(engine, "SELECT email, id FROM message_patient_accounts"))
    assert set(accounts) == {EMAIL}, "only an address with a confirmed link gets a backfill"
    account_id = accounts[EMAIL]
    member = dict(await _rows(engine, "SELECT id, account_id FROM message_patients"))
    by_name = {name: member[patients[name]] for name in names}
    assert by_name == {
        "login": None,
        "linked": account_id,
        "separate": None,
        "dead": None,
        "off": account_id,
        "other": None,
    }, "the first-contact consent alone joined an account"
    row_accounts = dict(
        await _rows(engine, "SELECT id, account_id FROM message_patient_sessions")
    )
    assert row_accounts[sessions["linked"][0]] == account_id
    assert row_accounts[sessions["login"][0]] is None, "the login joins when its cookie is used"
    codes = [row[0] for row in await _rows(engine, "SELECT patient_invite_code FROM tenants")]
    assert len(codes) == len(set(codes)) == len(names)
    assert all(len(code) == CODE_LENGTH and set(code) <= set(ALPHABET) for code in codes)

    # The cookie from before renews into what it reopened before: its clinic + the confirmed
    # link. Not the clinic logged into separately, not the dead one, not the channel-off one.
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    resp = await _refresh_with(sessionmaker, OLD_COOKIE)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    before = {str(tenants["login"]), str(tenants["linked"])}
    assert {c["tenant_id"] for c in body["clinics"]} == before
    assert {s["tenant_id"] for s in body["linked_sessions"]} == before
    assert {c["patient_ref"] for c in body["clinics"]} == {
        str(patients["login"]),
        str(patients["linked"]),
    }
    assert body["tenant_id"] == str(tenants["login"])
    assert body["patient_ref"] == str(patients["login"])
    assert decode_token(body["access_token"])["sid"] == str(sessions["login"][0])
    member = dict(await _rows(engine, "SELECT id, account_id FROM message_patients"))
    assert member[patients["login"]] == account_id
    assert member[patients["separate"]] is None and member[patients["dead"]] is None

    # The other device's cookie brings ITS clinic back too — and, the account being the
    # address, both devices now share every clinic (the stated consequence).
    resp = await _refresh_with(sessionmaker, SEPARATE_COOKIE)
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == str(tenants["separate"])
    assert {c["tenant_id"] for c in resp.json()["clinics"]} == before | {str(tenants["separate"])}

    _alembic("downgrade", "0019_patient_session_rotation")
    tables = {
        row[0]
        for row in await _rows(
            engine, "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        )
    }
    assert not tables & {"message_patient_accounts", "message_patient_account_otps"}
    tenant_columns = {
        row[0]
        for row in await _rows(
            engine, "SELECT column_name FROM information_schema.columns WHERE table_name='tenants'"
        )
    }
    assert "patient_invite_code" not in tenant_columns
    assert {row[0] for row in await _rows(engine, "SELECT id FROM message_patients")} == ids

    _alembic("upgrade", "head")
    again = dict(await _rows(engine, "SELECT id, account_id FROM message_patients"))
    assert set(again) == ids
    assert again[patients["linked"]] is not None and again[patients["off"]] is not None
    assert again[patients["login"]] is None and again[patients["separate"]] is None
    await engine.dispose()


async def test_the_postgres_only_statements_hold_under_real_concurrency():
    _guard()
    _alembic("upgrade", "head")
    engine = create_async_engine(PG_URL, poolclass=NullPool)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    address = f"concorrencia.{uuid.uuid4().hex[:8]}@exemplo.com"
    async with sessionmaker() as session, session.begin():
        tenant = Tenant(clinic_name="Clinica concorrencia", brain_message_enabled=True)
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id

    async def _on_own_connection(fn, *args):
        async with sessionmaker() as session:
            return await fn(session, *args)

    otp_rows = "SELECT count(*), max(attempts) FROM message_patient_account_otps WHERE email = :e"
    # Two first requests for a new address: no unique violation, one row.
    await asyncio.gather(
        *(_on_own_connection(patient_access.issue_account_otp, address) for _ in range(2))
    )
    assert await _rows(engine, otp_rows, {"e": address}) == [(1, 0)]

    # A burst of wrong guesses on separate connections spends exactly the ceiling.
    code = await _on_own_connection(patient_access.issue_account_otp, address)
    wrong = "000000" if code != "000000" else "111111"
    ceiling = get_settings().PATIENT_OTP_MAX_ATTEMPTS
    burst = await asyncio.gather(
        *(
            _on_own_connection(patient_access.verify_account_otp, address, wrong)
            for _ in range(ceiling * 3)
        )
    )
    assert not any(burst)
    assert await _rows(engine, otp_rows, {"e": address}) == [(1, ceiling)]
    assert await _on_own_connection(patient_access.verify_account_otp, address, code) is False

    # The right code, twice at once: one account opening.
    code = await _on_own_connection(patient_access.issue_account_otp, address)
    opened = await asyncio.gather(
        *(_on_own_connection(patient_access.verify_account_otp, address, code) for _ in range(2))
    )
    assert sorted(opened) == [False, True]

    accounts = await asyncio.gather(
        *(_on_own_connection(patient_access.open_account, address) for _ in range(3))
    )
    assert len({account.id for account in accounts}) == 1

    async def _add(_):
        async with sessionmaker() as session:
            account = await session.get(MessagePatientAccount, accounts[0].id)
            clinic = await session.get(Tenant, tenant_id)
            return await patient_access.add_clinic(session, account, clinic)

    added = await asyncio.gather(*(_add(i) for i in range(3)))
    assert len({patient.id for patient in added}) == 1
    identity_rows = "SELECT count(*) FROM message_patients WHERE tenant_id = :t AND email = :e"
    assert await _rows(engine, identity_rows, {"t": tenant_id, "e": address}) == [(1,)]
    consents = (
        "SELECT count(*) FROM patient_consent_events "
        "WHERE subject_ref = :r AND kind = 'brain_message_channel_access'"
    )
    assert await _rows(engine, consents, {"r": str(added[0].id)}) == [(1,)]
    await engine.dispose()
