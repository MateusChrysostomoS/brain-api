"""Real locks and separate connections in an opt-in disposable local database."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain_api.api.deps import Principal
from brain_api.models import StaffAccount, Tenant, User
from brain_api.services import team

URL = os.environ.get("TEAM_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="requires disposable local PostgreSQL")


@pytest_asyncio.fixture
async def team_pg():
    parsed = make_url(URL)
    assert parsed.host in ("localhost", "127.0.0.1")
    assert parsed.database.startswith("task058_")
    engine = create_async_engine(URL, pool_size=5)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE tenants, staff_accounts CASCADE"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        clinic = Tenant(clinic_name="Synthetic clinic")
        session.add(clinic)
        await session.flush()
        actor = User(
            tenant_id=clinic.id,
            name="Owner",
            email="owner@example.com",
            password_hash="!not-for-login",
            role="doctor",
            is_owner=True,
        )
        target = User(
            tenant_id=clinic.id,
            name="Doctor",
            email="doctor@example.com",
            password_hash="!not-for-login",
            role="doctor",
            is_manager=True,
            professional_id=uuid4(),
            access_revoked_at=datetime.now(UTC),
        )
        session.add_all([actor, target])
        await session.commit()
        principal = Principal(str(actor.id), clinic.id, "doctor", is_owner=True)
        yield factory, principal, target.id
    await engine.dispose()


async def test_concurrent_restores_apply_one_generation(team_pg):
    factory, principal, uid = team_pg

    async def restore():
        async with factory() as session:
            return await team.restore_member_access(session, principal, uid, 0)

    results = await asyncio.gather(restore(), restore())
    assert all(r.access_status == "active" and not r.is_manager for r in results)
    assert all(r.access_version == 1 for r in results)


async def test_revocation_waits_for_restore_and_stale_retry_cannot_resurrect(team_pg, monkeypatch):
    factory, principal, uid = team_pg
    locked = asyncio.Event()
    release = asyncio.Event()
    original = team.target_member

    async def pause(session, tenant_id, user_id):
        user = await original(session, tenant_id, user_id)
        if not locked.is_set():
            locked.set()
            await release.wait()
        return user

    monkeypatch.setattr(team, "target_member", pause)

    async def restore():
        async with factory() as session:
            return await team.restore_member_access(session, principal, uid, 0)

    async def revoke():
        async with factory() as session:
            return await team.revoke_member_access(session, principal, uid)

    restoring = asyncio.create_task(restore())
    await asyncio.wait_for(locked.wait(), timeout=5)
    revoking = asyncio.create_task(revoke())
    await asyncio.sleep(0.05)
    assert not revoking.done()
    release.set()
    restored, revoked = await asyncio.gather(restoring, revoking)
    assert restored.access_version == 1
    assert revoked.access_version == 2 and revoked.access_status == "revoked"
    async with factory() as session:
        with pytest.raises(HTTPException) as stale:
            await team.restore_member_access(session, principal, uid, 0)
        assert stale.value.detail == "team_member_changed"


async def test_two_managers_cannot_both_remove_last_management(team_pg):
    factory, _, _ = team_pg
    async with factory() as session:
        clinic = Tenant(clinic_name="Two managers")
        session.add(clinic)
        await session.flush()
        managers = [
            User(
                tenant_id=clinic.id,
                name=f"Manager {i}",
                email=f"m{i}@example.com",
                password_hash="!not-for-login",
                role="doctor",
                is_manager=True,
            )
            for i in (1, 2)
        ]
        session.add_all(managers)
        await session.commit()
        principals = [Principal(str(m.id), clinic.id, "doctor", is_manager=True) for m in managers]

    async def remove(principal):
        async with factory() as session:
            try:
                await team.set_member_management(session, principal, principal.user_id, False)
                return "removed"
            except HTTPException as exc:
                return exc.detail

    assert sorted(await asyncio.gather(*(remove(p) for p in principals))) == [
        "last_manager_protected",
        "removed",
    ]


async def test_concurrent_pending_restore_rotates_and_sends_only_once(team_pg, monkeypatch):
    from brain_api.config import get_settings
    from brain_api.core.security import hash_password, verify_password
    from brain_api.services import mailer

    factory, principal, uid = team_pg
    monkeypatch.setattr(get_settings(), "BRAIN_MESSAGE_PUBLIC_URL", "http://127.0.0.1:8957")
    deliveries = []

    async def send(_to, template, variables):
        deliveries.append((template, variables))
        return True

    monkeypatch.setattr(mailer, "send_email", send)
    async with factory() as session:
        user = await session.get(User, uid)
        account = StaffAccount(
            id=uid,
            email=user.email,
            name=user.name,
            password_hash=hash_password("OldTemporary9"),
            requires_password_change=True,
            temporary_password_expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(account)
        await session.flush()
        user.account_id = account.id
        user.requires_password_change = True
        user.temporary_password_expires_at = account.temporary_password_expires_at
        await session.commit()

    async def restore():
        async with factory() as session:
            return await team.restore_member_access(session, principal, uid, 0)

    results = await asyncio.gather(restore(), restore())
    assert all(r.access_status == "pending_first_access" and not r.is_manager for r in results)
    assert all(r.access_version == 1 for r in results)
    assert len(deliveries) == 1 and deliveries[0][0] == "staff_temporary_password"
    async with factory() as session:
        account = await session.get(StaffAccount, uid)
        user = await session.get(User, uid)
        assert account.credential_version == user.credential_version == 1
        assert not verify_password("OldTemporary9", account.password_hash)
        assert verify_password(deliveries[0][1]["temporary_password"], account.password_hash)
        assert account.requires_password_change and user.requires_password_change
