"""Who sees the whole clinic agenda in secretarIA's hub (owner decision 2026-10-09 A).

Ground truth: services/hub_scope.py (rule + live resolver), api/internal.py::verify_hub_token.
"""

from uuid import UUID, uuid4

import pytest

from brain_api.core.security import hash_password
from brain_api.models import Tenant, User
from brain_api.services.hub_scope import (
    AGENDA_SCOPE_CLINIC,
    AGENDA_SCOPE_OWN,
    agenda_scope_for,
    resolve_agenda_scope,
)

# --- the rule ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "is_owner", "is_manager", "expected"),
    [
        ("secretary", False, False, AGENDA_SCOPE_CLINIC),
        ("secretary", True, True, AGENDA_SCOPE_CLINIC),
        ("manager", False, False, AGENDA_SCOPE_CLINIC),
        ("doctor", True, False, AGENDA_SCOPE_CLINIC),
        ("doctor", False, True, AGENDA_SCOPE_CLINIC),
        ("doctor", False, False, AGENDA_SCOPE_OWN),
        ("tenant_owner", False, False, AGENDA_SCOPE_CLINIC),  # legacy token window
        ("tenant_staff", False, False, AGENDA_SCOPE_OWN),  # legacy token window
        ("admin", True, True, AGENDA_SCOPE_OWN),
        ("", False, False, AGENDA_SCOPE_OWN),
        ("Doctor", True, True, AGENDA_SCOPE_OWN),  # roles are exact lower-case strings
    ],
)
def test_the_rule(role, is_owner, is_manager, expected):
    assert agenda_scope_for(role, is_owner=is_owner, is_manager=is_manager) == expected


# --- the live resolver ---------------------------------------------------------------


async def _user(session, tenant_id, *, role="doctor", is_owner=False, is_manager=False) -> User:
    user = User(
        tenant_id=tenant_id,
        email=f"{uuid4().hex}@x.com",
        name="Pessoa",
        password_hash=hash_password("senha1234"),
        role=role,
        is_owner=is_owner,
        is_manager=is_manager,
    )
    session.add(user)
    await session.flush()
    return user


async def _tenant(session) -> Tenant:
    tenant = Tenant(clinic_name=f"Clínica {uuid4().hex[:6]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def test_the_resolver_reads_the_users_current_row(db_session):
    tenant = await _tenant(db_session)
    doctor = await _user(db_session, tenant.id)
    secretary = await _user(db_session, tenant.id, role="secretary")

    assert await resolve_agenda_scope(db_session, tenant.id, str(doctor.id)) == AGENDA_SCOPE_OWN
    assert (
        await resolve_agenda_scope(db_session, tenant.id, str(secretary.id)) == AGENDA_SCOPE_CLINIC
    )

    doctor.is_manager = True  # promoted after the token was minted: live, not from the token
    await db_session.flush()
    assert await resolve_agenda_scope(db_session, tenant.id, str(doctor.id)) == AGENDA_SCOPE_CLINIC


@pytest.mark.parametrize("actor", [None, "", "owner-a", "not-a-uuid", 42, str(uuid4())])
async def test_an_unknown_or_malformed_actor_is_own(db_session, actor):
    tenant = await _tenant(db_session)
    assert await resolve_agenda_scope(db_session, tenant.id, actor) == AGENDA_SCOPE_OWN


async def test_a_user_of_another_clinic_is_own_even_when_owner(db_session):
    clinic = await _tenant(db_session)
    other = await _tenant(db_session)
    foreign_owner = await _user(db_session, other.id, is_owner=True, is_manager=True)

    assert (
        await resolve_agenda_scope(db_session, clinic.id, str(foreign_owner.id)) == AGENDA_SCOPE_OWN
    )


# --- the introspection endpoint ------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

import brain_api.api.internal as internal_api  # noqa: E402
from brain_api.core.security import create_hub_token  # noqa: E402
from tests.test_rbac import (  # noqa: E402
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    CLINIC_A,
    CLINIC_B,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    _bearer,
    _token,
)

VERIFY = "/internal/secretaria/hub-token/verify"
PAIR = {"X-Internal-Api-Key": "pair-key"}


@pytest.fixture
def pair_key(monkeypatch):
    fake_settings = SimpleNamespace(SECRETARIA_API_KEY="pair-key", SECRETARIA_API_KEY_PREVIOUS="")
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)


async def _clinics(client) -> tuple[str, dict[str, str]]:
    admin = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin))).json()["items"]
    ids = {t["clinic_name"]: t["id"] for t in tenants}
    entitled = await client.patch(
        f"/admin/tenants/{ids[CLINIC_A]}/entitlements",
        headers=_bearer(admin),
        json={"plan": "complete_clinic_combo", "status": "active"},
    )
    assert entitled.status_code == 200, entitled.text
    return admin, ids


async def _create_user(client, admin, tenant_id, *, role, is_manager=False) -> str:
    response = await client.post(
        "/admin/users",
        headers=_bearer(admin),
        json={
            "email": f"{uuid4().hex[:10]}@clinica.com",
            "name": "Pessoa",
            "password": "senha1234",
            "role": role,
            "tenant_id": tenant_id,
            "is_manager": is_manager,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _owner_a_id(client, admin) -> str:
    users = (await client.get("/admin/users?limit=100", headers=_bearer(admin))).json()["items"]
    return next(u["id"] for u in users if u["email"] == OWNER_A_EMAIL)


async def _promote_to_manager(user_id: str) -> None:
    """The same in-memory DB the `client` fixture serves (its get_session override)."""
    from sqlalchemy import update

    from brain_api.core.database import get_session
    from brain_api.main import app

    session_gen = app.dependency_overrides[get_session]()
    session = await session_gen.__anext__()
    await session.execute(update(User).where(User.id == UUID(user_id)).values(role="manager"))
    await session.commit()
    await session_gen.aclose()


async def _verify(client, tenant_id, actor, **extra) -> dict:
    token = create_hub_token(tenant_id=tenant_id, actor_user_id=actor, **extra)
    response = await client.post(VERIFY, headers=PAIR, json={"token": token})
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_owner_sees_the_whole_clinic(client, pair_key):
    admin, ids = await _clinics(client)

    body = await _verify(client, ids[CLINIC_A], await _owner_a_id(client, admin))

    assert body["active"] is True and body["agenda_scope"] == "clinic"


@pytest.mark.parametrize(
    ("role", "is_manager", "expected"),
    [
        ("doctor", False, "own"),
        ("doctor", True, "clinic"),
        ("manager", False, "clinic"),
        ("secretary", False, "clinic"),
    ],
)
async def test_each_role_gets_its_scope(client, pair_key, role, is_manager, expected):
    admin, ids = await _clinics(client)
    user_id = await _create_user(client, admin, ids[CLINIC_A], role=role, is_manager=is_manager)

    body = await _verify(client, ids[CLINIC_A], user_id)

    assert body["agenda_scope"] == expected


async def test_a_promotion_counts_on_the_next_introspection_of_the_same_token(client, pair_key):
    admin, ids = await _clinics(client)
    user_id = await _create_user(client, admin, ids[CLINIC_A], role="doctor")
    token = create_hub_token(tenant_id=ids[CLINIC_A], actor_user_id=user_id)
    before = (await client.post(VERIFY, headers=PAIR, json={"token": token})).json()

    await _promote_to_manager(user_id)  # no admin route edits a role: write the row
    after = (await client.post(VERIFY, headers=PAIR, json={"token": token})).json()

    assert (before["agenda_scope"], after["agenda_scope"]) == ("own", "clinic")


async def test_an_actor_of_another_clinic_or_unknown_is_own(client, pair_key):
    admin, ids = await _clinics(client)
    clinic_b_manager = await _create_user(client, admin, ids[CLINIC_B], role="manager")

    assert (await _verify(client, ids[CLINIC_A], clinic_b_manager))["agenda_scope"] == "own"
    assert (await _verify(client, ids[CLINIC_A], "owner-a"))["agenda_scope"] == "own"
    assert (await _verify(client, ids[CLINIC_A], str(uuid4())))["agenda_scope"] == "own"


async def test_a_refused_token_is_still_a_200_with_own(client, pair_key):
    owner_jwt = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)

    as_user_jwt = await client.post(VERIFY, headers=PAIR, json={"token": owner_jwt})
    garbage = await client.post(VERIFY, headers=PAIR, json={"token": "not-a-jwt"})

    for response in (as_user_jwt, garbage):
        assert response.status_code == 200
        assert response.json() == {
            "active": False,
            "tenant_id": None,
            "professional_id": None,
            "agenda_scope": "own",
        }


async def test_professional_id_passthrough_is_unchanged(client, pair_key):
    admin, ids = await _clinics(client)
    professional_id = str(uuid4())

    body = await _verify(
        client, ids[CLINIC_A], await _owner_a_id(client, admin), professional_id=professional_id
    )

    assert body["professional_id"] == professional_id and body["tenant_id"] == ids[CLINIC_A]


@pytest.mark.parametrize("status", ["canceled", "inactive"])
async def test_inactive_entitlement_is_own_even_for_an_owner(client, pair_key, status):
    admin, ids = await _clinics(client)
    actor = await _owner_a_id(client, admin)
    professional_id = str(uuid4())
    changed = await client.patch(
        f"/admin/tenants/{ids[CLINIC_A]}/entitlements",
        headers=_bearer(admin),
        json={"status": status},
    )
    assert changed.status_code == 200, changed.text

    body = await _verify(client, ids[CLINIC_A], actor, professional_id=professional_id)

    assert body == {
        "active": False,
        "tenant_id": ids[CLINIC_A],
        "professional_id": professional_id,
        "agenda_scope": "own",
    }


async def test_secretaria_disabled_is_own_even_for_an_owner(client, pair_key):
    admin, ids = await _clinics(client)
    actor = await _owner_a_id(client, admin)
    changed = await client.patch(
        f"/admin/tenants/{ids[CLINIC_A]}/entitlements",
        headers=_bearer(admin),
        json={"plan": "precheck", "status": "active"},
    )
    assert changed.status_code == 200, changed.text

    body = await _verify(client, ids[CLINIC_A], actor)

    assert body["active"] is False and body["agenda_scope"] == "own"


@pytest.mark.parametrize("actor", [None, "", 42, [], {}])
async def test_malformed_actor_claim_is_own_without_changing_active(client, pair_key, actor):
    _, ids = await _clinics(client)

    body = await _verify(client, ids[CLINIC_A], actor)

    assert body["active"] is True and body["agenda_scope"] == "own"
