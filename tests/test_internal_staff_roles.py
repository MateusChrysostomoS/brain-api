"""Tests for `GET /internal/tenants/{tenant_id}/staff-roles` (TASK-052 Task 16, TASK-067 s06).

secretarIA asks "does this clinic have a working secretary?" when the clinic releases a
PAID slot: with one, the patient's decision deadline follows the CLINIC's closing time;
without one (or when the answer is unknown), the consultation DOCTOR's. The role in the hub
token of whoever presses "liberar" cannot answer that (it says who acts, not who the clinic
has), so brain-api — the identity authority — answers from its live rows.

"Active" is the team screen's own rule (`services/team.member_out` == "active") plus a
usable personal account (`services/team.require_remaining_manager`): not revoked, not
pending first access / invite / membership, and an account (when linked) neither disabled
nor waiting for its own first password.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import brain_api.api.internal as internal_api
from brain_api.core.database import Base, get_session
from brain_api.main import app
from brain_api.models import StaffAccount, Tenant, User

_PATH = "/internal/tenants/{tenant_id}/staff-roles"
_KEY = {"X-Internal-Api-Key": "pair-key"}


def _set_pair_key(monkeypatch, key: str = "pair-key", previous: str = "") -> None:
    fake_settings = SimpleNamespace(SECRETARIA_API_KEY=key, SECRETARIA_API_KEY_PREVIOUS=previous)
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)


def _user(tenant: Tenant, role: str, **kw) -> User:
    return User(
        tenant_id=tenant.id,
        email=f"{uuid4().hex[:10]}@clinica.example",
        name="Pessoa Sintética",
        password_hash="!",
        role=role,
        **kw,
    )


@pytest_asyncio.fixture
async def world():
    """A fresh DB and a client; tests seed exactly the rows they are about."""
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_get_session():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(client=client, sessionmaker=sessionmaker)
    app.dependency_overrides.clear()
    await engine.dispose()


async def _clinic(world, *members) -> Tenant:
    """One clinic whose members are `(role, extra_user_fields, account_fields | None)`."""
    async with world.sessionmaker() as session, session.begin():
        tenant = Tenant(clinic_name="Clínica Sintética")
        session.add(tenant)
        await session.flush()
        for role, fields, account_fields in members:
            account_id = None
            if account_fields is not None:
                account = StaffAccount(
                    email=f"{uuid4().hex[:10]}@conta.example",
                    name="Conta Sintética",
                    password_hash="!",
                    **account_fields,
                )
                session.add(account)
                await session.flush()
                account_id = account.id
            session.add(_user(tenant, role, account_id=account_id, **fields))
    return tenant


async def _roles(world, tenant_id) -> tuple[int, dict]:
    response = await world.client.get(_PATH.format(tenant_id=tenant_id), headers=_KEY)
    return response.status_code, response.json()


_OWNER = ("doctor", {"is_owner": True, "is_manager": True}, None)


# --- Pair-key gate ----------------------------------------------------------------------


async def test_key_unset_is_forbidden(world):
    """conftest forces SECRETARIA_API_KEY empty: fail CLOSED (403), never readable."""
    response = await world.client.get(_PATH.format(tenant_id=uuid4()), headers=_KEY)
    assert response.status_code == 403


async def test_a_wrong_key_is_unauthorized(world, monkeypatch):
    _set_pair_key(monkeypatch)
    response = await world.client.get(
        _PATH.format(tenant_id=uuid4()), headers={"X-Internal-Api-Key": "not-the-key"}
    )
    assert response.status_code == 401


async def test_no_key_header_is_unauthorized(world, monkeypatch):
    _set_pair_key(monkeypatch)
    response = await world.client.get(_PATH.format(tenant_id=uuid4()))
    assert response.status_code == 401


async def test_a_user_bearer_token_is_not_a_key(world, monkeypatch):
    """No user JWT is accepted on /internal/*: a browser session cannot read the roster."""
    _set_pair_key(monkeypatch)
    tenant = await _clinic(world, _OWNER)
    response = await world.client.get(
        _PATH.format(tenant_id=tenant.id), headers={"Authorization": "Bearer anything"}
    )
    assert response.status_code == 401


async def test_the_previous_key_works_during_a_rotation(world, monkeypatch):
    _set_pair_key(monkeypatch, key="new-key", previous="pair-key")
    tenant = await _clinic(world, _OWNER)
    assert (await _roles(world, tenant.id))[0] == 200


# --- The answer -------------------------------------------------------------------------


async def test_distinct_roles_of_the_active_members_sorted(world, monkeypatch):
    _set_pair_key(monkeypatch)
    tenant = await _clinic(
        world,
        _OWNER,
        ("doctor", {}, None),
        ("manager", {}, None),
        ("secretary", {}, None),
        ("secretary", {}, None),  # duplicated role -> listed once
    )
    assert await _roles(world, tenant.id) == (200, {"roles": ["doctor", "manager", "secretary"]})


async def test_a_clinic_without_a_secretary(world, monkeypatch):
    _set_pair_key(monkeypatch)
    tenant = await _clinic(world, _OWNER, ("doctor", {}, None))
    assert await _roles(world, tenant.id) == (200, {"roles": ["doctor"]})


async def test_the_answer_carries_only_roles_never_a_person(world, monkeypatch):
    """No name, e-mail, user id or count: the caller needs the presence of a role, nothing else."""
    _set_pair_key(monkeypatch)
    tenant = await _clinic(world, _OWNER, ("secretary", {}, None))
    status_code, body = await _roles(world, tenant.id)
    assert status_code == 200 and set(body) == {"roles"}
    assert all(isinstance(role, str) and "@" not in role for role in body["roles"])


async def test_a_linked_account_in_good_standing_counts(world, monkeypatch):
    _set_pair_key(monkeypatch)
    tenant = await _clinic(world, _OWNER, ("secretary", {}, {}))
    assert await _roles(world, tenant.id) == (200, {"roles": ["doctor", "secretary"]})


_NOW = datetime(2030, 3, 5, 12, 0, tzinfo=UTC)
INACTIVE_SECRETARIES = {
    "revoked": ({"access_revoked_at": _NOW}, None),
    "pending_first_access": ({"requires_password_change": True}, None),
    "pending_invite_token": ({"invite_token_hash": "a" * 64}, None),
    "membership_pending": ({"membership_pending": True}, None),
    "account_disabled": ({}, {"disabled_at": _NOW}),
    "account_first_password_pending": ({}, {"requires_password_change": True}),
}


async def test_a_secretary_who_cannot_act_does_not_count(world, monkeypatch):
    """Every non-active state of the team model, one secretary each, in ONE clinic: none of
    them makes the clinic "have a secretary"."""
    _set_pair_key(monkeypatch)
    tenant = await _clinic(
        world,
        _OWNER,
        *[("secretary", fields, account) for fields, account in INACTIVE_SECRETARIES.values()],
    )
    assert await _roles(world, tenant.id) == (200, {"roles": ["doctor"]})


async def test_each_inactive_state_alone_is_excluded(world, monkeypatch):
    _set_pair_key(monkeypatch)
    for fields, account in INACTIVE_SECRETARIES.values():
        tenant = await _clinic(world, ("secretary", fields, account))
        assert await _roles(world, tenant.id) == (200, {"roles": []})


async def test_a_revoked_role_next_to_an_active_one_still_counts(world, monkeypatch):
    _set_pair_key(monkeypatch)
    tenant = await _clinic(
        world,
        _OWNER,
        ("secretary", {"access_revoked_at": _NOW}, None),
        ("secretary", {}, None),
    )
    assert await _roles(world, tenant.id) == (200, {"roles": ["doctor", "secretary"]})


async def test_another_clinics_secretary_never_leaks(world, monkeypatch):
    _set_pair_key(monkeypatch)
    alfa = await _clinic(world, _OWNER)
    await _clinic(world, _OWNER, ("secretary", {}, None))
    assert await _roles(world, alfa.id) == (200, {"roles": ["doctor"]})


async def test_an_unknown_tenant_is_a_404(world, monkeypatch):
    """Unknown is not "no secretary": secretarIA reads 404 as unknown (doctor's closing)."""
    _set_pair_key(monkeypatch)
    status_code, body = await _roles(world, uuid4())
    assert status_code == 404 and body == {"detail": "tenant_not_found"}


async def test_a_malformed_tenant_id_is_a_422(world, monkeypatch):
    _set_pair_key(monkeypatch)
    response = await world.client.get(_PATH.format(tenant_id="not-a-uuid"), headers=_KEY)
    assert response.status_code == 422
