"""Restoration is a membership transition, never a personal credential reset."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from brain_api.core.security import hash_password
from brain_api.models import Tenant, User

pytest_plugins = ["tests.test_team"]


async def add_member(db, owner, **values):
    user = User(
        tenant_id=owner.tenant_id,
        name="Restored doctor",
        email=f"{uuid4()}@example.com",
        password_hash=hash_password("PersonalTest9"),
        role=values.pop("role", "doctor"),
        professional_id=values.pop("professional_id", uuid4()),
        **values,
    )
    db.add(user)
    await db.commit()
    return user


async def test_restore_drops_management_preserves_identity_and_old_credentials(team_client):
    c, owner, db, headers, emails = team_client
    user = await add_member(db, owner, is_manager=True)
    old = (
        await c.post("/auth/token", json={"email": user.email, "password": "PersonalTest9"})
    ).json()
    saved = (user.id, user.professional_id, user.password_hash)
    revoked = await c.post(f"/doctor/team/{user.id}/revoke", headers=headers)
    assert revoked.status_code == 200
    restored = await c.post(
        f"/doctor/team/{user.id}/restore",
        headers=headers,
        json={"expected_version": user.credential_version},
    )
    assert restored.status_code == 200
    assert restored.json()["access_status"] == "active"
    assert restored.json()["is_manager"] is False
    await db.refresh(user)
    assert (user.id, user.professional_id, user.password_hash) == saved
    assert not emails
    assert (
        await c.get("/auth/me", headers={"Authorization": f"Bearer {old['access_token']}"})
    ).status_code == 401
    assert (
        await c.post(
            "/auth/refresh",
            headers={"X-Brain-Client": "web"},
            json={"refresh_token": old["refresh_token"]},
        )
    ).status_code == 401
    new = await c.post("/auth/token", json={"email": user.email, "password": "PersonalTest9"})
    assert new.status_code == 200
    assert (
        await c.get(
            "/doctor/team", headers={"Authorization": f"Bearer {new.json()['access_token']}"}
        )
    ).status_code == 403
    granted = await c.patch(
        f"/doctor/team/{user.id}/management", headers=headers, json={"is_manager": True}
    )
    assert granted.status_code == 200 and granted.json()["is_manager"] is True


async def test_restore_is_idempotent_but_stale_restore_cannot_undo_new_revocation(team_client):
    c, owner, db, headers, _ = team_client
    user = await add_member(db, owner, access_revoked_at=datetime.now(UTC))
    payload = {"expected_version": user.credential_version}
    url = f"/doctor/team/{user.id}/restore"
    first = await c.post(url, headers=headers, json=payload)
    assert first.status_code == 200
    second = await c.post(url, headers=headers, json=payload)
    assert second.json() == first.json()
    assert (await c.post(f"/doctor/team/{user.id}/revoke", headers=headers)).status_code == 200
    assert (await c.post(url, headers=headers, json=payload)).status_code == 409
    await db.refresh(user)
    assert user.access_revoked_at


@pytest.mark.parametrize("role", ["manager", "secretary"])
async def test_restore_preserves_nonclinical_profile(team_client, role):
    c, owner, db, headers, _ = team_client
    user = await add_member(
        db,
        owner,
        role=role,
        professional_id=None,
        access_revoked_at=datetime.now(UTC),
        is_manager=role == "manager",
    )
    result = await c.post(
        f"/doctor/team/{user.id}/restore",
        headers=headers,
        json={"expected_version": user.credential_version},
    )
    assert result.status_code == 200
    assert result.json()["role"] == role
    assert result.json()["professional_id"] is None


async def test_restore_owner_scope_and_request_validation(team_client):
    c, owner, db, headers, _ = team_client
    url = f"/doctor/team/{owner.id}/restore"
    assert (await c.post(url, headers=headers, json={"expected_version": 0})).status_code == 409
    assert (
        await c.post(
            f"/doctor/team/{uuid4()}/restore", headers=headers, json={"expected_version": 0}
        )
    ).status_code == 404
    foreign_clinic = Tenant(clinic_name="Foreign synthetic clinic")
    db.add(foreign_clinic)
    await db.flush()
    foreign = User(
        tenant_id=foreign_clinic.id,
        name="Foreign manager",
        email="foreignrestore@example.com",
        password_hash="!not-for-login",
        role="manager",
        is_manager=True,
        access_revoked_at=datetime.now(UTC),
    )
    db.add(foreign)
    await db.commit()
    assert (
        await c.post(
            f"/doctor/team/{foreign.id}/restore", headers=headers, json={"expected_version": 0}
        )
    ).status_code == 404
    assert (
        await c.post(
            url, headers=headers, json={"expected_version": 0, "tenant_id": str(owner.tenant_id)}
        )
    ).status_code == 422
    owner.is_owner = owner.is_manager = False
    await db.commit()
    assert (await c.post(url, headers=headers, json={"expected_version": 0})).status_code == 403


async def test_management_removal_and_grant_are_separate_and_last_manager_protected(team_client):
    c, owner, db, headers, _ = team_client
    user = await add_member(db, owner, is_manager=True)
    for enabled in (False, True):
        result = await c.patch(
            f"/doctor/team/{user.id}/management", headers=headers, json={"is_manager": enabled}
        )
        assert result.status_code == 200 and result.json()["is_manager"] is enabled
        assert result.json()["professional_id"] == str(user.professional_id)
    owner.is_owner = False
    owner.is_manager = False
    await db.commit()
    member_token = (
        await c.post("/auth/token", json={"email": user.email, "password": "PersonalTest9"})
    ).json()["access_token"]
    last_headers = {"Authorization": f"Bearer {member_token}"}
    assert (
        await c.patch(
            f"/doctor/team/{user.id}/management", headers=last_headers, json={"is_manager": False}
        )
    ).status_code == 409
    assert (await c.post(f"/doctor/team/{user.id}/revoke", headers=last_headers)).status_code == 409


@pytest.mark.parametrize("expired", [False, True])
async def test_legacy_pending_restore_requires_new_proof_and_keeps_setup_obligation(
    team_client, expired
):
    c, owner, db, headers, emails = team_client
    created = (
        await c.post(
            "/doctor/team/invites",
            headers=headers,
            json={
                "name": "Pending",
                "email": "pending@example.com",
                "role": "doctor",
                "is_manager": True,
            },
        )
    ).json()
    uid = created["member"]["user_id"]
    old_password = emails[-1]["variables"]["temporary_password"]
    old_setup = (
        await c.post("/auth/token", json={"email": "pending@example.com", "password": old_password})
    ).json()["access_token"]
    if expired:
        target = await db.get(User, UUID(uid))
        target.temporary_password_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()
    revoked = (await c.post(f"/doctor/team/{uid}/revoke", headers=headers)).json()
    before_delivery = len(emails)
    result = await c.post(
        f"/doctor/team/{uid}/restore",
        headers=headers,
        json={"expected_version": revoked["access_version"]},
    )
    assert result.status_code == 200
    assert result.json()["access_status"] == "pending_first_access"
    assert result.json()["is_manager"] is False
    assert len(emails) == before_delivery + 1
    assert emails[-1]["template"] == "staff_temporary_password"
    assert (
        await c.post("/auth/token", json={"email": "pending@example.com", "password": old_password})
    ).status_code == 401
    assert (
        await c.post(
            "/auth/first-access/complete",
            headers={"Authorization": f"Bearer {old_setup}"},
            json={"new_password": "PersonalNew9"},
        )
    ).status_code == 401
    assert (
        await c.post(f"/doctor/team/{uid}/first-access/resend", headers=headers)
    ).status_code == 200
    password = emails[-1]["variables"]["temporary_password"]
    setup = (
        await c.post("/auth/token", json={"email": "pending@example.com", "password": password})
    ).json()
    assert setup["requires_password_change"]
    assert (
        await c.get("/auth/me", headers={"Authorization": f"Bearer {setup['access_token']}"})
    ).status_code == 401
    completed = await c.post(
        "/auth/first-access/complete",
        headers={"Authorization": f"Bearer {setup['access_token']}"},
        json={"new_password": "PersonalNew9"},
    )
    assert completed.status_code == 200


async def test_personal_recovery_then_restore_keeps_b_and_new_password(
    team_client, staff_accounts_enabled
):
    from brain_api.services.auth import (
        complete_password_reset,
        issue_password_reset_code,
        verify_password_reset_code,
    )
    from brain_api.services.staff_accounts import adopt_user

    c, owner, db, headers, _ = team_client
    user = await add_member(db, owner, is_manager=True)
    account = await adopt_user(db, user)
    b = Tenant(clinic_name="Fictional B")
    db.add(b)
    await db.flush()
    other = User(
        tenant_id=b.id,
        account_id=account.id,
        email=user.email,
        name=user.name,
        password_hash="!account-managed",
        role="doctor",
        is_manager=True,
        professional_id=uuid4(),
    )
    db.add(other)
    await db.commit()

    async def login_and_select(password):
        login = await c.post(
            "/auth/account/login", json={"email": user.email, "password": password}
        )
        assert login.status_code == 200
        proof = {"Authorization": f"Bearer {login.json()['account_token']}"}
        tokens = []
        for member in (user, other):
            result = await c.post(
                "/auth/account/select", headers=proof, json={"user_id": str(member.id)}
            )
            tokens.append(result)
        return login.json(), proof, tokens

    _, _, before = await login_and_select("PersonalTest9")
    assert all(r.status_code == 200 for r in before)
    old_a = before[0].json()["access_token"]
    await c.post(f"/doctor/team/{user.id}/revoke", headers=headers)
    assert (
        await c.get(
            "/auth/me", headers={"Authorization": f"Bearer {before[1].json()['access_token']}"}
        )
    ).status_code == 200
    issued = await issue_password_reset_code(db, user.email)
    proof_token = await verify_password_reset_code(db, user.email, issued[1])
    assert await complete_password_reset(db, proof_token, "NewPersonalTest9")
    login, proof, after = await login_and_select("NewPersonalTest9")
    assert (
        next(m for m in login["memberships"] if m["user_id"] == str(user.id))["status"] == "revoked"
    )
    assert after[0].status_code == 403 and after[1].status_code == 200
    b_token = after[1].json()["access_token"]
    await db.refresh(user)
    await db.refresh(account)
    snapshot = (
        account.password_hash,
        account.credential_version,
        account.reset_token_hash,
        other.id,
        other.professional_id,
        other.credential_version,
    )
    result = await c.post(
        f"/doctor/team/{user.id}/restore",
        headers=headers,
        json={"expected_version": user.credential_version},
    )
    assert result.status_code == 200 and result.json()["is_manager"] is False
    await db.refresh(account)
    await db.refresh(other)
    assert snapshot == (
        account.password_hash,
        account.credential_version,
        account.reset_token_hash,
        other.id,
        other.professional_id,
        other.credential_version,
    )
    assert (
        await c.get("/auth/me", headers={"Authorization": f"Bearer {old_a}"})
    ).status_code == 401
    assert (
        await c.get("/auth/me", headers={"Authorization": f"Bearer {b_token}"})
    ).status_code == 200
    selected = await c.post("/auth/account/select", headers=proof, json={"user_id": str(user.id)})
    assert selected.status_code == 200
    assert (
        await c.get(
            "/auth/me", headers={"Authorization": f"Bearer {selected.json()['access_token']}"}
        )
    ).status_code == 200


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("delivered", [False, True])
async def test_pending_personal_restore_automatically_renews_first_access(
    team_client, staff_accounts_enabled, mail_spy, expired, delivered
):
    from brain_api.services.staff_accounts import adopt_user

    c, owner, db, headers, emails = team_client
    user = await add_member(
        db,
        owner,
        is_manager=True,
        requires_password_change=True,
        temporary_password_expires_at=datetime.now(UTC) + timedelta(hours=72),
    )
    account = await adopt_user(db, user)
    b = Tenant(clinic_name="Other pending clinic")
    db.add(b)
    await db.flush()
    other = User(
        tenant_id=b.id,
        account_id=account.id,
        email=user.email,
        name=user.name,
        role="doctor",
        password_hash="!account-managed",
        membership_pending=True,
    )
    db.add(other)
    await db.commit()
    old_setup = (
        await c.post("/auth/account/login", json={"email": user.email, "password": "PersonalTest9"})
    ).json()["access_token"]
    if expired:
        account.temporary_password_expires_at = user.temporary_password_expires_at = datetime.now(
            UTC
        ) - timedelta(minutes=1)
        await db.commit()
    revoked = (await c.post(f"/doctor/team/{user.id}/revoke", headers=headers)).json()
    version = account.credential_version
    mail_spy.result = delivered
    before_delivery = len(emails)
    result = await c.post(
        f"/doctor/team/{user.id}/restore",
        headers=headers,
        json={"expected_version": revoked["access_version"]},
    )
    assert result.status_code == 200
    assert result.json()["access_status"] == "pending_first_access"
    assert result.json()["delivery_status"] == ("sent" if delivered else "failed")
    assert result.json()["is_manager"] is False
    assert len(emails) == before_delivery + 1
    assert emails[-1]["template"] == "staff_temporary_password"
    fresh_password = emails[-1]["variables"]["temporary_password"]
    assert fresh_password != "PersonalTest9"
    retry = await c.post(
        f"/doctor/team/{user.id}/restore",
        headers=headers,
        json={"expected_version": revoked["access_version"]},
    )
    assert retry.json() == result.json()
    assert len(emails) == before_delivery + 1
    await db.refresh(account)
    await db.refresh(user)
    assert account.credential_version == version + 1
    assert account.requires_password_change and user.requires_password_change
    assert (
        await c.post("/auth/account/login", json={"email": user.email, "password": "PersonalTest9"})
    ).status_code == 401
    assert (
        await c.post(
            "/auth/account/first-access/complete",
            headers={"Authorization": f"Bearer {old_setup}"},
            json={"new_password": "PersonalNew9"},
        )
    ).status_code == 401
    fresh_setup = await c.post(
        "/auth/account/login", json={"email": user.email, "password": fresh_password}
    )
    assert fresh_setup.status_code == 200
    assert fresh_setup.json()["requires_password_change"]
    setup_headers = {"Authorization": f"Bearer {fresh_setup.json()['access_token']}"}
    assert (await c.get("/auth/me", headers=setup_headers)).status_code == 401
    completed = await c.post(
        "/auth/account/first-access/complete",
        headers=setup_headers,
        json={"new_password": "PersonalNew9"},
    )
    assert completed.status_code == 200
    proof = {"Authorization": f"Bearer {completed.json()['account_token']}"}
    assert (
        await c.post("/auth/account/select", headers=proof, json={"user_id": str(other.id)})
    ).status_code == 409


async def test_existing_account_pending_invite_needs_fresh_acceptance_after_restore(
    team_client, staff_accounts_enabled, monkeypatch
):
    from brain_api.config import get_settings
    from brain_api.services.staff_accounts import adopt_user

    c, owner, db, headers, emails = team_client
    monkeypatch.setattr(get_settings(), "STAFF_MULTI_CLINIC_ENABLED", True)
    b = Tenant(clinic_name="Existing clinic B")
    db.add(b)
    await db.flush()
    existing = User(
        tenant_id=b.id,
        name="Person B",
        email="personb@example.com",
        role="doctor",
        password_hash=hash_password("PersonalTest9"),
    )
    db.add(existing)
    await db.flush()
    account = await adopt_user(db, existing)
    await db.commit()
    password_hash = account.password_hash
    invited = await c.post(
        "/doctor/team/invites",
        headers=headers,
        json={"name": "Person A", "email": existing.email, "role": "doctor", "is_manager": True},
    )
    assert invited.status_code == 201
    uid = invited.json()["member"]["user_id"]
    old_invite = emails[-1]["variables"]["link"].split("invite=")[1]
    revoked = (await c.post(f"/doctor/team/{uid}/revoke", headers=headers)).json()
    assert (
        await c.post(f"/doctor/team/{uid}/first-access/resend", headers=headers)
    ).status_code == 409
    restored = await c.post(
        f"/doctor/team/{uid}/restore",
        headers=headers,
        json={"expected_version": revoked["access_version"]},
    )
    assert restored.status_code == 200
    assert restored.json()["access_status"] == "pending_first_access"
    assert restored.json()["is_manager"] is False
    assert emails[-1]["variables"]["link"].split("invite=")[1] != old_invite
    assert restored.json()["delivery_status"] == "sent"
    for enabled in (True, False):
        managed = await c.patch(
            f"/doctor/team/{uid}/management", headers=headers, json={"is_manager": enabled}
        )
        assert managed.status_code == 200
        assert managed.json()["delivery_status"] == "not_requested"
    login = (
        await c.post(
            "/auth/account/login", json={"email": existing.email, "password": "PersonalTest9"}
        )
    ).json()
    proof = {"Authorization": f"Bearer {login['account_token']}"}
    assert (
        await c.post("/auth/account/invites/accept", headers=proof, json={"token": old_invite})
    ).status_code == 404
    assert (
        await c.post("/auth/account/select", headers=proof, json={"user_id": uid})
    ).status_code == 409
    resent = await c.post(f"/doctor/team/{uid}/first-access/resend", headers=headers)
    assert resent.status_code == 200
    fresh_invite = emails[-1]["variables"]["link"].split("invite=")[1]
    assert fresh_invite != old_invite
    assert (
        await c.post("/auth/account/invites/accept", headers=proof, json={"token": fresh_invite})
    ).status_code == 200
    selected = await c.post("/auth/account/select", headers=proof, json={"user_id": uid})
    assert selected.status_code == 200
    assert (
        await c.get(
            "/doctor/team", headers={"Authorization": f"Bearer {selected.json()['access_token']}"}
        )
    ).status_code == 403
    await db.refresh(account)
    assert account.password_hash == password_hash
