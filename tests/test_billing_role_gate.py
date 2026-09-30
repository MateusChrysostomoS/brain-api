"""Role gate on the billing routes that WRITE (TASK C, decision D4, spec §5.13)."""

from uuid import UUID, uuid4

import pytest

from brain_api.api.deps import Principal, is_billing_manager
from brain_api.core.security import hash_password
from brain_api.models import Tenant, User
from tests.test_billing import _tenant_ids
from tests.test_courtesy_coupon import _sessao
from tests.test_rbac import (
    CLINIC_A,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    _bearer,
    _token,
)

PASSWORD = "gatepass1234"

WRITE_ROUTES = [
    ("/billing/checkout", {"plan": "precheck_basic"}),
    ("/billing/portal", None),
    ("/billing/precheck/topup", {"quantity": 10}),
    ("/billing/precheck/upgrade", {"plan": "precheck_advanced"}),
]


async def _add_user(
    tenant_id: str, email: str, role: str, *, is_owner=False, is_manager=False
) -> None:
    async with _sessao() as session:
        session.add(
            User(
                tenant_id=UUID(tenant_id),
                email=email,
                name=email,
                password_hash=hash_password(PASSWORD),
                role=role,
                is_owner=is_owner,
                is_manager=is_manager,
            )
        )
        await session.commit()


def _principal(role: str, *, is_owner=False, is_manager=False) -> Principal:
    return Principal(
        user_id="u", tenant_id=uuid4(), role=role, is_owner=is_owner, is_manager=is_manager
    )


@pytest.mark.parametrize(
    ("role", "is_owner", "is_manager", "allowed"),
    [
        ("manager", False, False, True),
        ("doctor", True, False, True),  # the solo doctor-owner: the common buyer
        ("doctor", False, True, True),
        ("tenant_owner", False, False, True),  # legacy token in its transition window
        ("doctor", False, False, False),
        ("tenant_staff", False, False, False),
        ("secretary", False, False, False),
        ("secretary", True, True, False),  # never, even with the claims
        ("admin", False, False, False),
    ],
)
def test_is_billing_manager_matrix(role, is_owner, is_manager, allowed):
    assert is_billing_manager(_principal(role, is_owner=is_owner, is_manager=is_manager)) is allowed


@pytest.mark.parametrize(("path", "body"), WRITE_ROUTES)
@pytest.mark.parametrize("role", ["secretary", "doctor"])
async def test_secretary_and_plain_doctor_get_403_on_every_billing_write(client, path, body, role):
    tenant_a = (await _tenant_ids(client))[CLINIC_A]
    email = f"{role}@example.com"
    await _add_user(tenant_a, email, role)
    token = await _token(client, email, PASSWORD)

    resp = await client.post(path, headers=_bearer(token), json=body)

    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "billing_role_required"}


@pytest.mark.parametrize(("path", "body"), WRITE_ROUTES)
async def test_managers_and_owners_pass_the_gate(client, path, body):
    tenant_a = (await _tenant_ids(client))[CLINIC_A]
    await _add_user(tenant_a, "manager@example.com", "manager")
    await _add_user(tenant_a, "gestor@example.com", "doctor", is_manager=True)
    for email, password in (
        ("manager@example.com", PASSWORD),
        ("gestor@example.com", PASSWORD),
        (OWNER_A_EMAIL, OWNER_A_PASSWORD),
    ):
        token = await _token(client, email, password)
        resp = await client.post(path, headers=_bearer(token), json=body)
        assert resp.json().get("detail") != "billing_role_required", (email, resp.text)


async def test_reading_the_usage_stays_open_to_a_secretary(client):
    tenant_a = (await _tenant_ids(client))[CLINIC_A]
    await _add_user(tenant_a, "recepcao@example.com", "secretary")
    token = await _token(client, "recepcao@example.com", PASSWORD)
    resp = await client.get("/billing/precheck/usage", headers=_bearer(token))
    assert resp.status_code == 200, resp.text


async def test_a_test_clinic_gets_its_own_403_before_the_role_gate(client):
    tenant_a = (await _tenant_ids(client))[CLINIC_A]
    await _add_user(tenant_a, "recepcao@example.com", "secretary")
    async with _sessao() as session:
        tenant = await session.get(Tenant, UUID(tenant_a))
        tenant.is_test = True
        await session.commit()
    token = await _token(client, "recepcao@example.com", PASSWORD)
    resp = await client.post("/billing/portal", headers=_bearer(token))
    assert resp.status_code == 403
    assert resp.json() == {"detail": "test_tenant_billing_disabled"}
