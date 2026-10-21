"""Opt-in HTTP acceptance against tests/clinic_local_server.py, real local PostgreSQL."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain_api.core.security import hash_password
from brain_api.models import Entitlement, StaffAccount, StaffAccountResetCode, Tenant, User

BASE = os.environ.get("CLINIC_TEST_HTTP_URL", "")
pytestmark = pytest.mark.skipif(not BASE, reason="requires TASK-062 local HTTP simulator")


def snapshot(user):
    return (
        user.id,
        user.tenant_id,
        user.role,
        user.is_owner,
        user.is_manager,
        user.professional_id,
        user.access_revoked_at,
        user.credential_version,
    )


@pytest.mark.parametrize(
    "role,count,revoked",
    [
        ("doctor", 1, False),
        ("doctor", 2, False),
        ("manager", 1, False),
        ("doctor", 2, True),
        ("doctor", 0, False),
    ],
)
async def test_real_local_account_creates_and_pays_only_c(role, count, revoked):
    assert BASE == "http://127.0.0.1:8862"
    engine = create_async_engine("postgresql+asyncpg://postgres@127.0.0.1:55462/task062_browser")
    precheck_engine = create_async_engine(
        "postgresql+asyncpg://postgres@127.0.0.1:55462/task062_precheck"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    address = f"matrix-{uuid4().hex}@example.com"
    try:
        async with factory() as db:
            account = StaffAccount(
                email=address,
                name="Fictitious matrix person",
                password_hash=hash_password("LocalMatrix62"),
            )
            db.add(account)
            await db.flush()
            users = []
            for index in range(count):
                tenant = Tenant(clinic_name=f"Fictitious existing {index}")
                db.add(tenant)
                await db.flush()
                user = User(
                    tenant_id=tenant.id,
                    account_id=account.id,
                    email=address,
                    name=account.name,
                    role=role,
                    is_manager=role == "manager",
                    password_hash="!account-managed",
                    professional_id=uuid4(),
                    access_revoked_at=datetime.now(UTC) if revoked else None,
                )
                db.add(user)
                db.add(
                    Entitlement(
                        tenant_id=tenant.id,
                        status="active",
                        plan="precheck_basic",
                        stripe_customer_id="cus_old_" + tenant.id.hex,
                        stripe_subscription_id="sub_old_" + tenant.id.hex,
                    )
                )
                users.append(user)
            code = StaffAccountResetCode(
                account_id=account.id,
                code_hash="f" * 64,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                attempts=2,
                issued_in_window=3,
                window_started_at=datetime.now(UTC),
            )
            db.add(code)
            await db.commit()
            old = [snapshot(user) for user in users]
            credential = account.password_hash, account.credential_version
        async with httpx.AsyncClient(base_url=BASE) as client:
            login = await client.post(
                "/auth/account/login", json={"email": address, "password": "LocalMatrix62"}
            )
            assert login.status_code == 200
            headers = {"Authorization": "Bearer " + login.json()["account_token"]}
            operation = str(uuid4())
            data = {
                "operation_id": operation,
                "clinic_name": "Fictitious matrix C",
                "whatsapp_phone": "00062",
                "catalog_ids": ["precheck_basic"],
            }
            created = await client.post("/auth/account/clinics", headers=headers, json=data)
            assert created.status_code == 201
            repeated = await client.post("/auth/account/clinics", headers=headers, json=data)
            assert repeated.json()["intent_id"] == created.json()["intent_id"]
            clinic_session = created.json()["session"]
            me = await client.get(
                "/auth/me", headers={"Authorization": "Bearer " + clinic_session["access_token"]}
            )
            assert me.json()["tenant"]["clinic_name"] == "Fictitious matrix C"
            target = me.json()["tenant"]["id"]
            # Clinical proof cannot create another clinic, including owner proof for C.
            rejected = await client.post(
                "/auth/account/clinics",
                json={**data, "operation_id": str(uuid4())},
                headers={"Authorization": "Bearer " + clinic_session["access_token"]},
            )
            assert rejected.status_code == 401
            checkout = await client.post(
                f"/auth/account/clinics/{operation}/checkout", headers=headers
            )
            assert checkout.status_code == 200
            url = checkout.json()["checkout_url"]
            assert url.startswith("http://localhost:8864/checkout/cs_local_")
            sid = url.rsplit("/", 1)[1]
            paid = await client.post(f"http://127.0.0.1:8864/confirm/{sid}")
            assert paid.status_code == 303
            ready = await client.get(f"/auth/account/clinics/{operation}", headers=headers)
            assert ready.json()["status"] == "completed"
            # Real native PreCheck router + PostgreSQL, not the permissive spoke mock.
            sso = await client.post(
                "/sso/precheck/token",
                headers={"Authorization": "Bearer " + clinic_session["access_token"]},
            )
            assert sso.status_code == 200, sso.text
            native = await client.get(
                "http://127.0.0.1:8865/me",
                headers={"Authorization": "Bearer " + sso.json()["token"]},
            )
            assert native.status_code == 200, native.text
            async with precheck_engine.connect() as connection:
                linked_tenant = await connection.scalar(
                    text(
                        "SELECT c.brain_tenant_id FROM users u JOIN clinics c ON c.id=u.clinic_id "
                        "WHERE u.id=:user_id"
                    ),
                    {"user_id": native.json()["id"]},
                )
            assert linked_tenant == target
            assert native.json()["email"] == address
            assert native.json()["must_set_password"] is False
            memberships = (await client.get("/auth/account/memberships", headers=headers)).json()
            assert len(memberships) == count + 1
            for previous in old:
                member = next(m for m in memberships if m["user_id"] == str(previous[0]))
                assert member["status"] == ("revoked" if revoked else "active")
                if revoked:
                    still_denied = await client.post(
                        "/auth/account/select", headers=headers, json={"user_id": str(previous[0])}
                    )
                    assert still_denied.status_code == 403
            known = await client.post(
                "/public/signup-intents",
                json={
                    "name": "Stranger",
                    "email": address,
                    "password": "NotThePersonal62",
                    "clinic_name": "Unauthorized",
                    "whatsapp_phone": "00062",
                    "catalog_ids": ["precheck_basic"],
                },
            )
            assert known.status_code == 409
        async with factory() as db:
            for previous in old:
                user = await db.get(User, previous[0])
                assert snapshot(user) == previous
                ent = await db.get(Entitlement, previous[1])
                assert ent.stripe_customer_id == "cus_old_" + previous[1].hex
                assert ent.stripe_subscription_id == "sub_old_" + previous[1].hex
            person = await db.get(StaffAccount, account.id)
            assert (person.password_hash, person.credential_version) == credential
            challenge = await db.get(StaffAccountResetCode, account.id)
            assert (challenge.attempts, challenge.issued_in_window, challenge.code_hash) == (
                2,
                3,
                "f" * 64,
            )
            new = await db.scalar(
                select(User).where(User.tenant_id == __import__("uuid").UUID(target))
            )
            assert new.professional_id is None and new.is_owner and new.is_manager
            ent = await db.get(Entitlement, new.tenant_id)
            assert ent.status == "active" and ent.precheck_enabled
    finally:
        await engine.dispose()
        await precheck_engine.dispose()


async def test_new_account_keeps_anonymous_signup():
    assert BASE == "http://127.0.0.1:8862"
    async with httpx.AsyncClient(base_url=BASE) as client:
        result = await client.post(
            "/public/signup-intents",
            json={
                "name": "Fictitious new person",
                "email": f"new-{uuid4().hex}@example.com",
                "password": "LocalNewPerson62",
                "clinic_name": "Fictitious anonymous clinic",
                "whatsapp_phone": "00062",
                "catalog_ids": ["precheck_basic"],
            },
        )
        assert result.status_code == 201
        me = await client.get(
            "/auth/me",
            headers={"Authorization": "Bearer " + result.json()["session"]["access_token"]},
        )
        assert me.status_code == 200 and me.json()["user"]["is_owner"]
