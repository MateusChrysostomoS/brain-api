"""Disposable loopback PostgreSQL with independent connections, never production."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain_api.models import Entitlement, SignupIntent, StaffAccount, Tenant, User
from brain_api.schemas.signup import AccountClinicCreate
from brain_api.services import account_clinics, billing, signup, staff_accounts

URL = os.environ.get("CLINIC_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="requires disposable task062 PostgreSQL")


@pytest_asyncio.fixture
async def pg_clinic():
    parsed = make_url(URL)
    assert parsed.host in ("127.0.0.1", "localhost") and parsed.port == 55462
    assert parsed.database.startswith("task062_")
    engine = create_async_engine(URL, pool_size=20)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def seeded(factory):
    async with factory() as db:
        account = StaffAccount(
            email=f"{uuid4().hex}@example.com",
            name="Fictitious person",
            password_hash="!not-used-in-this-proof",
        )
        a, b = Tenant(clinic_name="Fictitious A"), Tenant(clinic_name="Fictitious B")
        db.add_all([account, a, b])
        await db.flush()
        for tenant in (a, b):
            db.add(
                User(
                    tenant_id=tenant.id,
                    account_id=account.id,
                    email=account.email,
                    name=account.name,
                    password_hash="!account-managed",
                    role="doctor",
                    professional_id=uuid4(),
                    access_revoked_at=datetime.now(UTC),
                )
            )
            db.add(
                Entitlement(
                    tenant_id=tenant.id,
                    status="active",
                    plan="precheck_basic",
                    stripe_customer_id=f"cus_old_{tenant.id.hex[:12]}",
                    stripe_subscription_id=f"sub_old_{tenant.id.hex[:12]}",
                )
            )
        await db.commit()
        row, _ = await staff_accounts.open_account_session(db, account)
        return account.id, staff_accounts.account_token(account, row), (a.id, b.id)


async def test_concurrent_create_checkout_and_webhooks(pg_clinic, monkeypatch):
    account_id, token, old_tenants = await seeded(pg_clinic)
    body = AccountClinicCreate(
        operation_id=uuid4(),
        clinic_name="Fictitious C",
        whatsapp_phone="0000000062",
        catalog_ids=["precheck_basic"],
    )

    async def create():
        async with pg_clinic() as db:
            result = await account_clinics.create_clinic(
                db, await staff_accounts.resolve_account(db, token), body
            )
            return result.intent.id, result.user.tenant_id

    results = await asyncio.gather(*(create() for _ in range(12)))
    assert len(set(results)) == 1
    intent_id, tenant_id = results[0]
    calls = []

    async def post(path, data, *, idempotency_key=None):
        await asyncio.sleep(0.02)
        calls.append((path, dict(data), idempotency_key))
        if path == "/v1/checkout/sessions":
            return {
                "id": "cs_" + intent_id.hex,
                "url": "https://checkout.example.test/" + intent_id.hex,
            }
        assert path == "/v1/subscriptions"
        assert data["customer"] == "cus_C" and data["metadata[tenant_id]"] == str(tenant_id)
        return {"id": "sub_C", "status": "active"}

    async def get(path):
        if path.startswith("/v1/checkout/sessions/"):
            return {"status": "open", "url": "https://checkout.example.test/" + intent_id.hex}
        assert path == "/v1/setup_intents/seti_C"
        return {"payment_method": "pm_fictitious_C"}

    monkeypatch.setattr(billing, "_stripe_post", post)
    monkeypatch.setattr(billing, "_stripe_get", get)

    async def checkout():
        async with pg_clinic() as db:
            await account_clinics.lock_proof(db, await staff_accounts.resolve_account(db, token))
            return await signup.create_checkout_session_for_intent(
                db, intent_id, owner_account_id=account_id
            )

    assert len(set(await asyncio.gather(*(checkout() for _ in range(8))))) == 1
    assert len(calls) == 1
    obj = {
        "id": "cs_" + intent_id.hex,
        "setup_intent": "seti_C",
        "customer": "cus_C",
        "metadata": {"kind": "signup_intent", "signup_intent_id": str(intent_id)},
    }
    event_id = "evt_" + uuid4().hex

    async def webhook(event):
        async with pg_clinic() as db:
            return await billing.apply_stripe_event(db, event, "checkout.session.completed", obj)

    # Same event twice concurrently, plus a later distinct delivery of the same payment.
    outcomes = await asyncio.gather(webhook(event_id), webhook(event_id))
    assert sorted(outcomes) == [False, True]
    assert await webhook(event_id + "_again") is True
    assert len(calls) == 2  # one checkout and one real subscription-create code path
    assert calls[1][2] == "signup-sub-" + str(intent_id)
    async with pg_clinic() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SignupIntent)
                .where(SignupIntent.owner_account_id == account_id)
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count()).select_from(User).where(User.account_id == account_id)
            )
            == 3
        )
        ent = await db.get(Entitlement, tenant_id)
        assert ent.precheck_enabled and ent.stripe_subscription_id == "sub_C"
        for old_id in old_tenants:
            ent = await db.get(Entitlement, old_id)
            assert ent.stripe_customer_id == f"cus_old_{old_id.hex[:12]}"
            assert ent.stripe_subscription_id == f"sub_old_{old_id.hex[:12]}"
            user = await db.scalar(select(User).where(User.tenant_id == old_id))
            assert user.access_revoked_at is not None and user.professional_id is not None


async def test_checkout_transport_failure_and_expired_retry_keep_c(pg_clinic, monkeypatch):
    from fastapi import HTTPException

    account_id, token, _ = await seeded(pg_clinic)
    async with pg_clinic() as db:
        body = AccountClinicCreate(
            operation_id=uuid4(),
            clinic_name="Retry C",
            whatsapp_phone="000",
            catalog_ids=["precheck_basic"],
        )
        result = await account_clinics.create_clinic(
            db, await staff_accounts.resolve_account(db, token), body
        )
        intent_id, tenant_id = result.intent.id, result.user.tenant_id
    keys = []

    async def failing(*args, idempotency_key=None, **kwargs):
        keys.append(idempotency_key)
        raise HTTPException(502, "stripe_unavailable")

    monkeypatch.setattr(billing, "_stripe_post", failing)
    async with pg_clinic() as db:
        with pytest.raises(HTTPException):
            await signup.create_checkout_session_for_intent(
                db, intent_id, owner_account_id=account_id
            )

    async def succeeding(*args, idempotency_key=None, **kwargs):
        keys.append(idempotency_key)
        return {"id": f"cs_retry_{len(keys)}", "url": "https://checkout.example.test/retry"}

    monkeypatch.setattr(billing, "_stripe_post", succeeding)
    async with pg_clinic() as db:
        await signup.create_checkout_session_for_intent(db, intent_id, owner_account_id=account_id)
    assert keys[0] == keys[1]

    async def expired(path):
        return {"status": "expired"}

    monkeypatch.setattr(billing, "_stripe_get", expired)
    async with pg_clinic() as db:
        await signup.create_checkout_session_for_intent(db, intent_id, owner_account_id=account_id)
        assert (await db.get(SignupIntent, intent_id)).tenant_id == tenant_id
        assert (await db.get(Entitlement, tenant_id)).status == "inactive"
    assert keys[2] == f"signup-checkout-{intent_id}-1"
