"""Real PostgreSQL, distinct connections. Opt-in and restricted to a disposable local DB."""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain_api.config import get_settings
from brain_api.core.security import hash_password, hash_refresh_token, verify_password
from brain_api.models import (
    StaffAccount,
    StaffAccountResetCode,
    StaffMembershipInvite,
    Tenant,
    User,
)
from brain_api.services import staff_accounts
from brain_api.services.auth import issue_refresh_token, rotate_refresh_token
from brain_api.services.staff_account_recovery import find_account, issue_code, verify_code
from brain_api.services.staff_invitations import accept_invitation

URL = os.environ.get("STAFF_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="requires disposable local PostgreSQL")
ROOT = Path(__file__).resolve().parents[1]


def migration(*arguments):
    env = {**os.environ, "DATABASE_URL": URL, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]


@pytest_asyncio.fixture
async def pg():
    parsed = make_url(URL)
    assert parsed.host in ("127.0.0.1", "localhost")
    assert parsed.database.startswith("task057_")
    migration("upgrade", "head")
    engine = create_async_engine(URL, pool_size=10)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE users, tenants, staff_accounts CASCADE"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def seed_memberships(factory):
    async with factory() as session:
        a, b = Tenant(clinic_name="Clinic A"), Tenant(clinic_name="Clinic B")
        session.add_all([a, b])
        await session.flush()
        account = StaffAccount(
            email="person@example.com", name="Person", password_hash=hash_password("Password123")
        )
        session.add(account)
        await session.flush()
        ua = User(
            account_id=account.id,
            tenant_id=a.id,
            email=account.email,
            name="Person",
            password_hash="!account-managed",
            role="doctor",
            is_manager=True,
            professional_id=uuid4(),
        )
        ub = User(
            account_id=account.id,
            tenant_id=b.id,
            email=account.email,
            name="Person",
            password_hash="!account-managed",
            role="doctor",
            is_manager=False,
            professional_id=uuid4(),
        )
        session.add_all([ua, ub])
        await session.commit()
        return account.id, ua.id, ub.id


async def test_postgres_selection_against_revocation(pg):
    aid, uaid, ubid = await seed_memberships(pg)
    selected = asyncio.Event()

    async def choose():
        async with pg() as session:
            account = await session.get(StaffAccount, aid)
            ua = await staff_accounts.select_membership(session, account, uaid)
            version = ua.credential_version
            selected.set()
            await asyncio.sleep(0.05)
            await session.commit()
            return version

    async def revoke():
        await selected.wait()
        async with pg() as session:
            ua = await session.scalar(select(User).where(User.id == uaid).with_for_update())
            ua.access_revoked_at = datetime.now(UTC)
            ua.credential_version += 1
            await session.commit()

    version, _ = await asyncio.gather(choose(), revoke())
    async with pg() as session:
        account = await session.get(StaffAccount, aid)
        with pytest.raises(HTTPException):
            await staff_accounts.select_membership(session, account, uaid)
        b = await staff_accounts.select_membership(session, account, ubid)
        assert not b.is_manager
        assert (await session.get(User, uaid)).credential_version != version


async def test_postgres_invite_acceptance_once(pg):
    aid, _, ubid = await seed_memberships(pg)
    token = "local-one-use-invite"
    async with pg() as session:
        user = await session.get(User, ubid)
        user.membership_pending = True
        session.add(
            StaffMembershipInvite(
                account_id=aid,
                tenant_id=user.tenant_id,
                user_id=user.id,
                membership_version=user.credential_version,
                name=user.name,
                role=user.role,
                is_manager=False,
                professional_id=user.professional_id,
                token_hash=hash_refresh_token(token),
                expires_at=datetime.now(UTC) + timedelta(hours=72),
            )
        )
        await session.commit()

    async def accept():
        async with pg() as session:
            account = await session.get(StaffAccount, aid)
            try:
                await accept_invitation(session, account, token)
                return True
            except HTTPException:
                return False

    assert sorted(await asyncio.gather(accept(), accept())) == [False, True]


async def test_postgres_refresh_replay_and_code_attempts(pg):
    aid, uaid, _ = await seed_memberships(pg)
    async with pg() as session:
        refresh = await issue_refresh_token(session, uaid)

    async def rotate():
        async with pg() as session:
            return await rotate_refresh_token(session, refresh)

    results = await asyncio.gather(rotate(), rotate())
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result)
    async with pg() as session:
        assert await rotate_refresh_token(session, winner.new_refresh_token) is None
        account = await find_account(session, "person@example.com")
        issued = await issue_code(session, account)
        assert issued

    async def guess():
        async with pg() as session:
            account = await find_account(session, "person@example.com")
            return await verify_code(session, account, "wrong!")

    assert not any(await asyncio.gather(*(guess() for _ in range(12))))
    async with pg() as session:
        row = await session.get(StaffAccountResetCode, aid)
        assert row.attempts == get_settings().PASSWORD_RESET_CODE_MAX_ATTEMPTS


async def test_postgres_normalized_email_concurrency(pg):
    async def reserve(email):
        async with pg() as session:
            session.add(StaffAccount(email=email, name="Person", password_hash="test-only"))
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    assert sorted(
        await asyncio.gather(reserve(" PERSON@example.com "), reserve("person@EXAMPLE.com"))
    ) == [False, True]


async def test_postgres_legacy_migration_proofs(pg):
    # Seed precisely the pre-account shape, with separate valid challenge and reset proof.
    migration("downgrade", "0030_staff_password_reset_codes")
    uid, tid, pid = uuid4(), uuid4(), uuid4()
    raw_reset, raw_refresh = "legacy-reset-test-only", "legacy-refresh-test-only"
    from brain_api.services.auth import _hash_reset_code

    engine = create_async_engine(URL)
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO tenants (id,clinic_name) VALUES (:id,'Clinic A')"), {"id": tid}
        )
        await connection.execute(
            text("""INSERT INTO users
            (id,tenant_id,email,name,password_hash,role,professional_id,reset_token_hash,reset_token_expires_at)
            VALUES (:id,:tid,'person@example.com','Person',:hash,'doctor',:pid,:reset,
                    now()+interval '15 minutes')"""),
            {
                "id": uid,
                "tid": tid,
                "pid": pid,
                "hash": hash_password("Password123"),
                "reset": hash_refresh_token(raw_reset),
            },
        )
        await connection.execute(
            text("""INSERT INTO staff_password_reset_codes
            VALUES (:id,:hash,now()+interval '10 minutes',2,now(),4)"""),
            {"id": uid, "hash": _hash_reset_code("123456")},
        )
        await connection.execute(
            text("""INSERT INTO refresh_tokens (id,user_id,token_hash,expires_at)
            VALUES (:id,:uid,:hash,now()+interval '1 day')"""),
            {"id": uuid4(), "uid": uid, "hash": hash_refresh_token(raw_refresh)},
        )
        await connection.execute(
            text("""INSERT INTO precheck_account_links
            (id,brain_user_id,precheck_user_id,tenant_id) VALUES (:id,:uid,12345,:tid)"""),
            {"id": uuid4(), "uid": uid, "tid": tid},
        )
    migration("upgrade", "0031_staff_accounts")
    async with engine.begin() as connection:
        count = await connection.scalar(text("SELECT count(*) FROM staff_accounts"))
        assert count == 0  # Expansion must leave old writers' credential/proof ownership intact.
        assert (
            await connection.scalar(
                text("SELECT issued_in_window FROM staff_password_reset_codes WHERE user_id=:id"),
                {"id": uid},
            )
            == 4
        )
        await connection.execute(
            text("UPDATE users SET password_hash=:hash WHERE id=:id"),
            {"id": uid, "hash": hash_password("LegacyChanged123")},
        )
        await connection.execute(
            text(
                "UPDATE staff_password_reset_codes SET attempts=3,issued_in_window=5 "
                "WHERE user_id=:id"
            ),
            {"id": uid},
        )
    migration("upgrade", "head")
    async with pg() as session:
        user = await session.get(User, uid)
        account = await session.get(StaffAccount, uid)
        code = await session.get(StaffAccountResetCode, uid)
        assert user.professional_id == pid and user.account_id == uid
        assert code.attempts == 3 and code.issued_in_window == 5
        assert account.reset_token_hash == hash_refresh_token(raw_reset)
        password_moved = verify_password("LegacyChanged123", account.password_hash)
        assert password_moved
        assert (await rotate_refresh_token(session, raw_refresh)).user.id == uid
    migration("downgrade", "0030_staff_password_reset_codes")
    migration("upgrade", "head")
    async with pg() as session:
        assert (await session.get(User, uid)).professional_id == pid
        assert (await session.get(StaffAccountResetCode, uid)).issued_in_window == 5
        assert (
            await session.scalar(
                text("SELECT precheck_user_id FROM precheck_account_links WHERE brain_user_id=:id"),
                {"id": uid},
            )
            == 12345
        )
    await engine.dispose()


async def test_postgres_personal_refresh_replay_does_not_kill_later_generation(pg):
    aid, _, _ = await seed_memberships(pg)
    async with pg() as session:
        account = await session.get(StaffAccount, aid)
        _, raw = await staff_accounts.open_account_session(session, account)

    async def rotate():
        async with pg() as session:
            try:
                return await staff_accounts.rotate_account_session(session, raw)
            except HTTPException:
                return None

    results = await asyncio.gather(rotate(), rotate())
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result)
    async with pg() as session:
        with pytest.raises(HTTPException):
            await staff_accounts.live_session(session, winner[1].id)
        account = await session.get(StaffAccount, aid)
        fresh, _ = await staff_accounts.open_account_session(session, account)
        with pytest.raises(HTTPException):
            await staff_accounts.rotate_account_session(session, raw)
        assert (await staff_accounts.live_session(session, fresh.id))[0].id == aid


async def test_postgres_downgrade_refuses_to_discard_multiple_memberships(pg):
    aid, uaid, ubid = await seed_memberships(pg)
    async with pg() as session:
        user = await session.get(User, uaid)
        user.access_revoked_at = datetime.now(UTC)
        await session.commit()
    env = {**os.environ, "DATABASE_URL": URL, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0031_staff_accounts"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "unsafe downgrade" in result.stderr
    async with pg() as session:
        assert (await session.get(User, uaid)).access_revoked_at
        assert (await session.get(User, ubid)).account_id == aid
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0032_staff_membership_email"
        )


async def test_postgres_clinical_old_generation_replay_preserves_new_refresh(pg):
    aid, uid, _ = await seed_memberships(pg)
    async with pg() as session:
        old = await issue_refresh_token(session, uid)
        await staff_accounts.end_account_sessions(session, aid)
        await session.commit()
        fresh = await issue_refresh_token(session, uid)
    async with pg() as session:
        assert await rotate_refresh_token(session, old) is None
    async with pg() as session:
        assert await rotate_refresh_token(session, fresh) is not None


@pytest.mark.parametrize(
    "flow", ["invite", "clinical_setup", "account_setup", "account_setup_after_helper"]
)
async def test_postgres_reset_between_proof_commit_and_session_emission(
    pg, monkeypatch, staff_accounts_enabled, flow
):
    from httpx import ASGITransport, AsyncClient

    from brain_api.core.database import get_session
    from brain_api.main import create_app
    from brain_api.services.auth import complete_password_reset
    from brain_api.services.staff_first_access import create_staff_setup_token

    aid, uid, _ = await seed_memberships(pg)
    reset = "reset-test-only"
    invite = "invite-test-only"
    async with pg() as session:
        account = await session.get(StaffAccount, aid)
        user = await session.get(User, uid)
        account.reset_token_hash = hash_refresh_token(reset)
        account.reset_token_expires_at = datetime.now(UTC) + timedelta(minutes=15)
        if flow == "invite":
            user.invite_token_hash = hash_refresh_token(invite)
            user.invite_token_expires_at = datetime.now(UTC) + timedelta(hours=1)
        else:
            account.requires_password_change = user.requires_password_change = True
            account.temporary_password_expires_at = user.temporary_password_expires_at = (
                datetime.now(UTC) + timedelta(hours=72)
            )
        await session.commit()
        setup = create_staff_setup_token(user) if flow != "invite" else None
    # Each operation uses a different real PostgreSQL connection. The barrier is the
    # first helper commit, before its post-commit read/emission resumes.
    async with pg() as request_session:
        commit = request_session.commit
        interrupted = False

        async def reset_after_proof():
            nonlocal interrupted
            if interrupted:
                return
            interrupted = True
            async with pg() as reset_session:
                if flow != "invite":
                    # Setup clears outstanding reset proofs, so emulate a newly
                    # verified recovery after that completion, before emission.
                    account = await reset_session.get(StaffAccount, aid)
                    account.reset_token_hash = hash_refresh_token(reset)
                    account.reset_token_expires_at = datetime.now(UTC) + timedelta(minutes=15)
                    await reset_session.commit()
                assert await complete_password_reset(reset_session, reset, "ResetPassword456")

        if flow == "account_setup_after_helper":
            from brain_api.services import staff_first_access

            original = staff_first_access.complete_staff_first_access

            async def complete_then_reset(*args, **kwargs):
                result = await original(*args, **kwargs)
                await reset_after_proof()
                return result

            monkeypatch.setattr(
                staff_first_access, "complete_staff_first_access", complete_then_reset
            )
        else:

            async def commit_then_reset():
                await commit()
                await reset_after_proof()

            monkeypatch.setattr(request_session, "commit", commit_then_reset)
        app = create_app()

        async def dependency():
            yield request_session

        app.dependency_overrides[get_session] = dependency
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            if flow == "invite":
                response = await client.post("/auth/exchange-invite-token", json={"token": invite})
            else:
                route = (
                    "/auth/first-access/complete"
                    if flow == "clinical_setup"
                    else "/auth/account/first-access/complete"
                )
                response = await client.post(
                    route,
                    headers={"Authorization": f"Bearer {setup}"},
                    json={"new_password": "SetupPassword123"},
                )
            assert interrupted
            assert response.status_code == 401


async def test_postgres_privacy_erases_and_exports_account_without_memberships(pg):
    from brain_api.services.privacy import _erase_brain_user, _export_brain_user

    aid, _, _ = await seed_memberships(pg)
    async with pg() as session:
        await session.execute(text("DELETE FROM users WHERE account_id=:id"), {"id": aid})
        account = await session.get(StaffAccount, aid)
        row, _ = await staff_accounts.open_account_session(session, account)
        await issue_code(session, account)
        exported = await _export_brain_user(session, account.email)
        assert exported["account"]["id"] == str(aid)
        assert exported["account"]["name"] == "Person"
        assert "password_hash" not in exported["account"]
        await _erase_brain_user(session, account.email)
        assert await session.get(StaffAccount, aid, populate_existing=True) is None
        from brain_api.models import StaffAccountSession

        assert await session.get(StaffAccountSession, row.id, populate_existing=True) is None
        assert await session.get(StaffAccountResetCode, aid, populate_existing=True) is None


async def test_postgres_revoked_only_account_recovers_without_clinical_token(
    pg, staff_accounts_enabled
):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import update

    from brain_api.core.database import get_session
    from brain_api.main import create_app
    from brain_api.services.auth import complete_password_reset

    aid, uid, _ = await seed_memberships(pg)
    async with pg() as session:
        await session.execute(
            update(User).where(User.account_id == aid).values(access_revoked_at=datetime.now(UTC))
        )
        account = await find_account(session, "person@example.com")
        account.preferred_user_id = uid
        _, code = await issue_code(session, account)
        token = await verify_code(session, account, code)
        assert await complete_password_reset(session, token, "RecoveredPassword789")
    app = create_app()

    async def dependency():
        async with pg() as session:
            yield session

    app.dependency_overrides[get_session] = dependency
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        assert (
            await client.post(
                "/auth/account/login",
                json={"email": "person@example.com", "password": "Password123"},
            )
        ).status_code == 401
        response = await client.post(
            "/auth/account/login",
            json={"email": "person@example.com", "password": "RecoveredPassword789"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["selection_state"] == "preferred_unavailable"
        assert body["clinical_session"] is None
        assert all(m["status"] == "revoked" for m in body["memberships"])
        proof = {"Authorization": f"Bearer {body['account_token']}"}
        assert (await client.get("/auth/me", headers=proof)).status_code == 401
        assert (
            await client.post("/auth/account/select", headers=proof, json={"user_id": str(uid)})
        ).status_code == 403


async def test_postgres_legacy_login_cannot_upgrade_password_proof_during_reset(pg, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from brain_api.core.database import get_session
    from brain_api.main import create_app
    from brain_api.services.auth import complete_password_reset

    aid, uid, other_uid = await seed_memberships(pg)
    async with pg() as session:
        await session.execute(text("DELETE FROM users WHERE id=:id"), {"id": other_uid})
        account = await session.get(StaffAccount, aid)
        account.reset_token_hash = hash_refresh_token("race-reset-only")
        account.reset_token_expires_at = datetime.now(UTC) + timedelta(minutes=15)
        await session.commit()
    async with pg() as request_session:
        original = request_session.scalars
        interrupted = False

        async def query_memberships_after_reset(*args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                async with pg() as reset_session:
                    assert await complete_password_reset(
                        reset_session, "race-reset-only", "ResetPassword999"
                    )
            return await original(*args, **kwargs)

        monkeypatch.setattr(request_session, "scalars", query_memberships_after_reset)
        app = create_app()

        async def dependency():
            yield request_session

        app.dependency_overrides[get_session] = dependency
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            response = await client.post(
                "/auth/token", json={"email": "person@example.com", "password": "Password123"}
            )
            assert interrupted
            assert response.status_code == 401


async def test_postgres_unlinked_null_and_expired_refresh_do_not_kill_versioned_successor(pg):
    from brain_api.models import RefreshToken

    async with pg() as session:
        user = User(
            email="legacy-admin@example.com", name="Admin", role="admin", password_hash="test-only"
        )
        session.add(user)
        await session.flush()
        session.add(
            RefreshToken(
                user_id=user.id,
                credential_version=None,
                token_hash=hash_refresh_token("null-legacy-only"),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await session.commit()
        rotated = await rotate_refresh_token(session, "null-legacy-only")
        assert rotated is not None
        # Null is supported only for a still-unlinked legacy identity. Once rotated,
        # its old unversioned proof cannot revoke the versioned successor.
        assert await rotate_refresh_token(session, "null-legacy-only") is None
        newer = await rotate_refresh_token(session, rotated.new_refresh_token)
        assert newer is not None
        expired = await session.scalar(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(rotated.new_refresh_token)
            )
        )
        expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        assert await rotate_refresh_token(session, rotated.new_refresh_token) is None
        assert await rotate_refresh_token(session, newer.new_refresh_token) is not None
