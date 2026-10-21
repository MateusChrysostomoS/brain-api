"""New-clinic purchase is authorized by personal proof, never the old clinic."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from brain_api.core.database import get_session
from brain_api.main import app
from brain_api.models import Entitlement, StaffAccount, Tenant, User
from tests.test_rbac import (
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    OWNER_B_EMAIL,
    OWNER_B_PASSWORD,
    _bearer,
)
from tests.test_signup import _register_body


async def proof(client):
    result = await client.post(
        "/auth/account/login",
        json={
            "email": OWNER_A_EMAIL,
            "password": OWNER_A_PASSWORD,
        },
    )
    assert result.status_code == 200
    return result.json()


def payload(operation=None):
    return {
        "operation_id": operation or str(uuid4()),
        "clinic_name": "Clínica fictícia C",
        "whatsapp_phone": "+5511000000062",
        "catalog_ids": ["precheck_basic"],
    }


@pytest.mark.parametrize(
    "role,revoked,count",
    [
        ("doctor", False, 1),
        ("doctor", False, 2),
        ("manager", False, 1),
        ("doctor", True, 2),
    ],
)
async def test_personal_account_creates_only_c(client, role, revoked, count):
    account = await proof(client)
    headers = _bearer(account["account_token"])
    old_id = account["memberships"][0]["user_id"]
    async for db in app.dependency_overrides[get_session]():
        old = await db.scalar(select(User).where(User.email == OWNER_A_EMAIL))
        old.role, old.is_owner, old.is_manager = role, False, role == "manager"
        old.access_revoked_at = datetime.now(UTC) if revoked else None
        if count == 2:
            b = Tenant(clinic_name="Outra clínica fictícia")
            db.add(b)
            await db.flush()
            db.add(
                User(
                    tenant_id=b.id,
                    account_id=old.account_id,
                    email=old.email,
                    name=old.name,
                    role="doctor",
                    password_hash="!account-managed",
                    access_revoked_at=old.access_revoked_at,
                    professional_id=uuid4(),
                )
            )
        await db.commit()
        rows = list(await db.scalars(select(User).where(User.account_id == old.account_id)))
        for row in rows:
            await db.refresh(row)  # snapshot persisted timestamps (SQLite drops tzinfo)
        before = [
            (
                u.id,
                u.tenant_id,
                u.role,
                u.is_owner,
                u.is_manager,
                u.access_revoked_at,
                u.professional_id,
                u.password_hash,
            )
            for u in rows
        ]
        personal = await db.get(StaffAccount, old.account_id)
        credential = personal.password_hash, personal.credential_version
    body = payload()
    created = await client.post("/auth/account/clinics", json=body, headers=headers)
    assert created.status_code == 201, created.text
    data = created.json()
    me = (await client.get("/auth/me", headers=_bearer(data["session"]["access_token"]))).json()
    assert me["tenant"]["clinic_name"] == "Clínica fictícia C"
    assert me["user"]["is_owner"] is True
    assert data["session"]["refresh_token"] is None
    repeated = await client.post("/auth/account/clinics", json=body, headers=headers)
    assert repeated.status_code == 201
    assert repeated.json()["intent_id"] == data["intent_id"]
    resumed = await client.get("/auth/account/clinics/" + body["operation_id"], headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()["intent_id"] == data["intent_id"]
    async for db in app.dependency_overrides[get_session]():
        for previous in before:
            u = await db.get(User, previous[0])
            assert (
                u.id,
                u.tenant_id,
                u.role,
                u.is_owner,
                u.is_manager,
                u.access_revoked_at,
                u.professional_id,
                u.password_hash,
            ) == previous
        personal = await db.get(StaffAccount, personal.id)
        assert (personal.password_hash, personal.credential_version) == credential
        owners = list(await db.scalars(select(User).where(User.account_id == personal.id)))
        assert len(owners) == count + 1
        new = next(u for u in owners if str(u.id) != old_id and u.is_owner)
        assert new.professional_id is None
        ent = await db.get(Entitlement, new.tenant_id)
        assert ent.status == "inactive"
        assert not ent.precheck_enabled and not ent.secretaria_enabled
        assert ent.stripe_customer_id is None and ent.stripe_subscription_id is None
    # Public signup continues to refuse takeover, even with the correct old email.
    anonymous = await client.post(
        "/public/signup-intents", json=_register_body(email=OWNER_A_EMAIL)
    )
    assert anonymous.status_code == 409
    assert anonymous.json()["detail"] == "email_already_registered"
    # Personal membership list contains the original rows and C with their statuses.
    memberships = (await client.get("/auth/account/memberships", headers=headers)).json()
    assert len(memberships) == count + 1
    assert next(m for m in memberships if m["user_id"] == old_id)["status"] == (
        "revoked" if revoked else "active"
    )


async def test_creation_requires_personal_proof_and_fixed_body(client):
    account = await proof(client)
    headers = _bearer(account["account_token"])
    selected = await client.post(
        "/auth/account/select",
        headers=headers,
        json={"user_id": account["memberships"][0]["user_id"]},
    )
    for invalid in ({}, _bearer(selected.json()["access_token"])):
        assert (
            await client.post("/auth/account/clinics", headers=invalid, json=payload())
        ).status_code == 401
    for field in ("email", "password", "tenant_id", "is_owner"):
        body = payload()
        body[field] = "untrusted"
        assert (
            await client.post("/auth/account/clinics", headers=headers, json=body)
        ).status_code == 422
    body = payload()
    assert (
        await client.post("/auth/account/clinics", headers=headers, json=body)
    ).status_code == 201
    body["clinic_name"] = "Other"
    assert (
        await client.post("/auth/account/clinics", headers=headers, json=body)
    ).status_code == 409


async def test_anonymous_race_keeps_conflict_response(client, monkeypatch):
    from brain_api.services import staff_accounts

    async def raced(*args):
        return False

    monkeypatch.setattr(staff_accounts, "email_registered", raced)
    response = await client.post("/public/signup-intents", json=_register_body(email=OWNER_A_EMAIL))
    assert response.status_code == 409


async def test_operation_is_private_and_later_c_revocation_is_respected(client, monkeypatch):
    from uuid import UUID

    from brain_api.services import billing

    async def forbidden(*args, **kwargs):
        pytest.fail("unauthorized operation reached Stripe")

    monkeypatch.setattr(billing, "_stripe_post", forbidden)
    a = await proof(client)
    body = payload()
    created = (
        await client.post("/auth/account/clinics", headers=_bearer(a["account_token"]), json=body)
    ).json()
    other = (
        await client.post(
            "/auth/account/login", json={"email": OWNER_B_EMAIL, "password": OWNER_B_PASSWORD}
        )
    ).json()
    path = "/auth/account/clinics/" + body["operation_id"]
    assert (await client.get(path, headers=_bearer(other["account_token"]))).status_code == 404
    assert (
        await client.post(path + "/checkout", headers=_bearer(other["account_token"]))
    ).status_code == 404
    async for db in app.dependency_overrides[get_session]():
        user = await db.get(User, UUID(created["session"]["user_id"]))
        user.access_revoked_at = datetime.now(UTC)
        await db.commit()
    assert (await client.get(path, headers=_bearer(a["account_token"]))).status_code == 403
    assert (
        await client.post(path + "/checkout", headers=_bearer(a["account_token"]))
    ).status_code == 403


async def test_account_intent_is_not_a_public_billing_credential(client, monkeypatch):
    from brain_api.services import billing

    async def forbidden_network(*args, **kwargs):
        pytest.fail("unauthorized intent reached Stripe")

    monkeypatch.setattr(billing, "_stripe_post", forbidden_network)
    account = await proof(client)
    headers = _bearer(account["account_token"])
    created = (await client.post("/auth/account/clinics", json=payload(), headers=headers)).json()
    intent_id = created["intent_id"]
    for path, body in [
        ("/public/checkout-sessions", {"intent_id": intent_id}),
        ("/public/courtesy-redemptions", {"intent_id": intent_id, "code": "LOCAL"}),
    ]:
        assert (await client.post(path, json=body)).status_code == 404
    assert (
        await client.patch(
            "/public/signup-intents/" + intent_id, json={"catalog_ids": ["precheck_basic"]}
        )
    ).status_code == 404


async def test_checkout_retries_reuse_c_and_only_activate_c(client, monkeypatch):
    from brain_api.models import SignupIntent
    from brain_api.services import billing
    from tests.test_billing import _event, _post_webhook

    calls = []

    async def stripe_post(path, data, *, idempotency_key=None):
        assert path == "/v1/checkout/sessions"
        assert data["mode"] == "setup" and data["customer_creation"] == "always"
        assert "customer" not in data
        calls.append((data, idempotency_key))
        return {"id": "cs_local_C", "url": "https://checkout.example.test/C"}

    async def stripe_get(path):
        assert path == "/v1/checkout/sessions/cs_local_C"
        return {"status": "open", "url": "https://checkout.example.test/C"}

    monkeypatch.setattr(billing, "_stripe_post", stripe_post)
    monkeypatch.setattr(billing, "_stripe_get", stripe_get)
    account = await proof(client)
    headers = _bearer(account["account_token"])
    body = payload()
    result = await client.post("/auth/account/clinics", json=body, headers=headers)
    assert result.status_code == 201
    intent_id = result.json()["intent_id"]
    path = "/auth/account/clinics/" + body["operation_id"] + "/checkout"
    for _ in range(2):
        checkout = await client.post(path, headers=headers)
        assert checkout.status_code == 200, checkout.text
        assert checkout.json()["checkout_url"] == "https://checkout.example.test/C"
    assert len(calls) == 1
    assert calls[0][1] == "signup-checkout-" + intent_id + "-0"
    assert "/criar-clinica?" in calls[0][0]["cancel_url"]
    assert body["operation_id"] in calls[0][0]["success_url"]
    async for db in app.dependency_overrides[get_session]():
        intent = await db.get(SignupIntent, __import__("uuid").UUID(intent_id))
        target = intent.tenant_id
        assert (await db.get(Entitlement, target)).status == "inactive"
    event = _event(
        "evt_local_C",
        "checkout.session.completed",
        {
            "id": "cs_local_C",
            "mode": "subscription",
            "customer": "cus_only_C",
            "subscription": "sub_only_C",
            "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
        },
    )
    assert (await _post_webhook(client, event)).status_code == 200
    assert (await _post_webhook(client, event)).json()["duplicate"] is True
    async for db in app.dependency_overrides[get_session]():
        ent = await db.get(Entitlement, target)
        assert ent.stripe_customer_id == "cus_only_C"
        assert ent.stripe_subscription_id == "sub_only_C"
        assert ent.precheck_enabled
    status = await client.get("/auth/account/clinics/" + body["operation_id"], headers=headers)
    assert status.json()["status"] == "completed"
    # Public status must not mint a new login credential for this personal account.
    assert (await client.get("/public/onboarding-status?session_id=cs_local_C")).status_code == 404


async def test_operational_c_cannot_start_parallel_existing_clinic_purchase(client, monkeypatch):
    from brain_api.services import billing

    async def unexpected(*args, **kwargs):
        pytest.fail("a parallel checkout escaped the personal operation")

    monkeypatch.setattr(billing, "_stripe_post", unexpected)
    account = await proof(client)
    created = await client.post(
        "/auth/account/clinics", json=payload(), headers=_bearer(account["account_token"])
    )
    session = created.json()["session"]
    checkout = await client.post(
        "/billing/checkout",
        headers=_bearer(session["access_token"]),
        json={"plan": "precheck_basic", "addons": []},
    )
    assert checkout.status_code == 409
    assert checkout.json()["detail"] == "signup_checkout_required"


async def test_webhook_preserves_c_if_another_subscription_became_active(client, monkeypatch):
    from uuid import UUID

    from brain_api.models import SignupIntent
    from brain_api.services import billing
    from tests.test_billing import _event, _post_webhook

    account = await proof(client)
    created = (
        await client.post(
            "/auth/account/clinics", json=payload(), headers=_bearer(account["account_token"])
        )
    ).json()
    async for db in app.dependency_overrides[get_session]():
        intent = await db.get(SignupIntent, UUID(created["intent_id"]))
        intent.stripe_session_id = "cs_pending_C"
        tenant_id = intent.tenant_id
        ent = await db.get(Entitlement, tenant_id)
        ent.status, ent.stripe_customer_id, ent.stripe_subscription_id = (
            "active",
            "cus_existing_C",
            "sub_existing_C",
        )
        await db.commit()

    async def unexpected(*args, **kwargs):
        pytest.fail("an active C subscription must be preserved")

    monkeypatch.setattr(billing, "_stripe_post", unexpected)

    async def setup(*args, **kwargs):
        return {"payment_method": "pm_C"}

    monkeypatch.setattr(billing, "_stripe_get", setup)
    event = _event(
        "evt_pending_C",
        "checkout.session.completed",
        {
            "id": "cs_pending_C",
            "setup_intent": "seti_C",
            "customer": "cus_other_C",
            "metadata": {"kind": "signup_intent", "signup_intent_id": created["intent_id"]},
        },
    )
    assert (await _post_webhook(client, event)).status_code == 200
    async for db in app.dependency_overrides[get_session]():
        ent = await db.get(Entitlement, tenant_id)
        assert (ent.stripe_customer_id, ent.stripe_subscription_id) == (
            "cus_existing_C",
            "sub_existing_C",
        )


async def test_webhook_recovers_its_subscription_committed_before_the_intent(client, monkeypatch):
    from uuid import UUID

    from brain_api.models import SignupIntent
    from brain_api.services import billing
    from tests.test_billing import _event, _post_webhook

    account = await proof(client)
    data = (
        await client.post(
            "/auth/account/clinics", json=payload(), headers=_bearer(account["account_token"])
        )
    ).json()
    async for db in app.dependency_overrides[get_session]():
        intent = await db.get(SignupIntent, UUID(data["intent_id"]))
        intent.stripe_session_id = "cs_recovery_C"
        tenant_id = intent.tenant_id
        ent = await db.get(Entitlement, tenant_id)
        ent.status, ent.stripe_customer_id, ent.stripe_subscription_id = (
            "active",
            "cus_own_C",
            "sub_own_C",
        )
        await db.commit()

    async def existing(path):
        assert path == "/v1/subscriptions/sub_own_C"
        return {
            "id": "sub_own_C",
            "status": "active",
            "customer": "cus_own_C",
            "metadata": {"signup_intent_id": data["intent_id"]},
        }

    async def forbidden(*args, **kwargs):
        pytest.fail("recovery must reuse the subscription, never charge again")

    monkeypatch.setattr(billing, "_stripe_get", existing)
    monkeypatch.setattr(billing, "_stripe_post", forbidden)
    response = await _post_webhook(
        client,
        _event(
            "evt_recovery_C",
            "checkout.session.completed",
            {
                "id": "cs_recovery_C",
                "customer": "cus_own_C",
                "setup_intent": "seti_own_C",
                "metadata": {"kind": "signup_intent", "signup_intent_id": data["intent_id"]},
            },
        ),
    )
    assert response.status_code == 200
    async for db in app.dependency_overrides[get_session]():
        assert (await db.get(SignupIntent, UUID(data["intent_id"]))).status == "completed"


async def test_failed_unbilled_intent_restarts_checkout_without_new_clinic(client, monkeypatch):
    from uuid import UUID

    from brain_api.models import SignupIntent
    from brain_api.services import billing

    account = await proof(client)
    headers = _bearer(account["account_token"])
    body = payload()
    data = (await client.post("/auth/account/clinics", json=body, headers=headers)).json()
    async for db in app.dependency_overrides[get_session]():
        intent = await db.get(SignupIntent, UUID(data["intent_id"]))
        target = intent.tenant_id
        intent.status, intent.failure_reason, intent.stripe_session_id = (
            "failed",
            "subscription_create_failed",
            "cs_failed_C",
        )
        await db.commit()

    async def previous(path):
        return {"status": "complete"}

    async def new_checkout(path, data, *, idempotency_key=None):
        assert path == "/v1/checkout/sessions" and idempotency_key.endswith("-1")
        return {"id": "cs_retried_C", "url": "https://checkout.example.test/retried"}

    monkeypatch.setattr(billing, "_stripe_get", previous)
    monkeypatch.setattr(billing, "_stripe_post", new_checkout)
    response = await client.post(
        "/auth/account/clinics/" + body["operation_id"] + "/checkout", headers=headers
    )
    assert response.status_code == 200
    async for db in app.dependency_overrides[get_session]():
        intent = await db.get(SignupIntent, UUID(data["intent_id"]))
        assert intent.tenant_id == target and intent.status == "pending_payment"
        assert intent.failure_reason is None
        assert (await db.get(Entitlement, target)).status == "inactive"
