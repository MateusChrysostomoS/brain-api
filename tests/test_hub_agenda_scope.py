"""Who sees the whole clinic agenda in secretarIA's hub (owner decision 2026-10-09 A).

Ground truth: services/hub_scope.py (rule + live resolver), api/internal.py::verify_hub_token.
"""

from uuid import uuid4

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
