"""`secretary` role tests (secretary round, 2026-08-14).

The role is defined as much by what it CANNOT do as by what it can, so this module is
written as two matrices against a live `secretary` session:

REACHES (product decision: full power inside secretarIA) — the operational portal, the
clinic's agenda, team management (inviting doctors AND other secretaries), billing, and
the onboarding pause that is owner-only for everyone else.

REFUSED (the secretarIA-only boundary) — the three `deny_secretary` call sites:
`POST /sso/precheck/token`, `GET /doctor/anamneses{,/id}` (PreCheck clinical data), and
`POST /doctor/professionals/self` (which would make a receptionist bookable).

The SSO case is deliberately set up so the tenant IS PreCheck-entitled: a doctor on this
same tenant gets 409 `precheck_account_not_linked` (it reached the service), while the
secretary gets 403 `secretary_precheck_not_allowed` — proving the exclusion fires FIRST
and is a real gate, not a side effect of the tenant lacking an entitlement.

Same hermetic setup as tests/test_rbac.py: real app, in-memory aiosqlite, mesh upstreams
unset (conftest) so proxy routes degrade instead of hitting the network.
"""

from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from brain_api.core.database import Base, get_session
from brain_api.core.security import decode_token, hash_password
from brain_api.main import app
from brain_api.models import Entitlement, Tenant, User
from brain_api.models.user import (
    ROLE_ADMIN,
    ROLE_DOCTOR,
    ROLE_SECRETARY,
    ROLES,
)
from brain_api.services import onboarding_sync, secretaria_provisioning

CLINIC = "Clínica Secretária"
ADMIN_EMAIL, ADMIN_PASSWORD = "admin@brain.co", "adminpass1"
OWNER_EMAIL, OWNER_PASSWORD = "owner@clinic.com", "ownerpass1"
# A plain doctor: NOT the owner. Guards the regression that widening `require_owner` for
# `secretary` did not accidentally open it to every doctor.
STAFF_EMAIL, STAFF_PASSWORD = "staff@clinic.com", "staffpass1"
SECRETARY_EMAIL, SECRETARY_PASSWORD = "recepcao@clinic.com", "secretarypass1"


@pytest_asyncio.fixture
async def client():
    """One tenant with an owner doctor, a non-owner doctor, and a secretary.

    The tenant is entitled to BOTH products — PreCheck entitlement is what makes the SSO
    assertion meaningful (see the module docstring).
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with sessionmaker() as session, session.begin():
        session.add(
            User(
                tenant_id=None,
                email=ADMIN_EMAIL,
                name="Brain Co Admin",
                password_hash=hash_password(ADMIN_PASSWORD),
                role=ROLE_ADMIN,
            )
        )
        tenant = Tenant(clinic_name=CLINIC)
        session.add(tenant)
        await session.flush()
        session.add_all(
            [
                User(
                    tenant_id=tenant.id,
                    email=OWNER_EMAIL,
                    name="Dra. Dona",
                    password_hash=hash_password(OWNER_PASSWORD),
                    role=ROLE_DOCTOR,
                    is_owner=True,
                    is_manager=True,
                    professional_id=uuid4(),
                ),
                User(
                    tenant_id=tenant.id,
                    email=STAFF_EMAIL,
                    name="Dr. Empregado",
                    password_hash=hash_password(STAFF_PASSWORD),
                    role=ROLE_DOCTOR,
                    professional_id=uuid4(),
                ),
                User(
                    tenant_id=tenant.id,
                    email=SECRETARY_EMAIL,
                    name="Rita Recepção",
                    password_hash=hash_password(SECRETARY_PASSWORD),
                    role=ROLE_SECRETARY,
                    # The defining absence: no professional linkage, ever.
                    professional_id=None,
                ),
                Entitlement(
                    tenant_id=tenant.id,
                    precheck_enabled=True,
                    secretaria_enabled=True,
                    plan="complete_clinic_combo",
                    status="active",
                ),
            ]
        )

    async def _override_get_session():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.tenant_id = tenant.id
        c.sessionmaker = sessionmaker
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


async def _token(client, email, password) -> str:
    resp = await client.post("/auth/token", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- Taxonomy --------------------------------------------------------------


def test_secretary_is_a_known_role():
    """The constant is wired into the canonical tuple, not just defined next to it."""
    assert ROLE_SECRETARY == "secretary"
    assert ROLE_SECRETARY in ROLES


async def test_secretary_can_log_in_and_token_carries_the_role(client):
    """A secretary authenticates like anyone else; the claim survives into the JWT."""
    token = await _token(client, SECRETARY_EMAIL, SECRETARY_PASSWORD)
    claims = decode_token(token)
    assert claims["role"] == ROLE_SECRETARY
    assert claims["tenant_id"] == str(client.tenant_id)
    # Never a professional -> the claim is absent/None, so nothing downstream (hub token,
    # agenda) can mistake a receptionist for someone patients book.
    assert not claims.get("professional_id")


# --- REACHES: the operational portal ---------------------------------------


async def test_secretary_reaches_the_operational_portal(client, monkeypatch):
    """Every day-to-day surface answers a secretary token: profile, the clinic's agenda
    and patients, the professionals list, billing and entitlements."""
    monkeypatch.setattr(onboarding_sync, "refresh_config_status", _noop_async(None))
    token = await _token(client, SECRETARY_EMAIL, SECRETARY_PASSWORD)

    for route in (
        "/doctor/me",
        "/doctor/appointments",
        "/doctor/patients",
        "/doctor/professionals",
        "/doctor/secretaries",
        "/entitlements",
        "/billing/precheck/usage",
    ):
        resp = await client.get(route, headers=_bearer(token))
        assert resp.status_code == 200, f"{route} -> {resp.status_code} {resp.text}"


async def test_secretary_can_pause_onboarding_but_a_plain_doctor_still_cannot(client):
    """`require_owner` accepts `secretary` as an ALTERNATIVE to `is_owner` — without
    turning into "any doctor", which is what the non-owner half asserts."""
    secretary = await _token(client, SECRETARY_EMAIL, SECRETARY_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/pause", json={"retries": True}, headers=_bearer(secretary)
    )
    assert resp.status_code == 200, resp.text

    staff = await _token(client, STAFF_EMAIL, STAFF_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/pause", json={"retries": True}, headers=_bearer(staff)
    )
    assert resp.status_code == 403

    owner = await _token(client, OWNER_EMAIL, OWNER_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/pause", json={"retries": False}, headers=_bearer(owner)
    )
    assert resp.status_code == 200, resp.text


# --- REACHES: team management ----------------------------------------------


async def test_secretary_invites_a_secretary_without_creating_a_professional(client, monkeypatch):
    """The invite mirrors the professional one EXCEPT for the secretaria round-trip: no
    `professionals` row, no `professional_id`, and the reused `professional_invite`
    template."""
    emails: list[dict] = []

    async def fake_email(to, template, variables):
        emails.append({"to": to, "template": template, "variables": variables})
        return True

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("create_professional must NOT be called for a secretary")

    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", fake_email)
    monkeypatch.setattr(secretaria_provisioning, "create_professional", must_not_be_called)

    token = await _token(client, SECRETARY_EMAIL, SECRETARY_PASSWORD)
    resp = await client.post(
        "/doctor/secretaries/invites",
        json={"name": "Nova Recepção", "email": "Nova@Clinic.com"},
        headers=_bearer(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "invite_link" in body and body["invite_link"]
    # No `professional_id` key at all — the schema does not carry one.
    assert "professional_id" not in body

    async with client.sessionmaker() as session:
        invited = await session.scalar(select(User).where(User.email == "nova@clinic.com"))
    assert invited is not None
    assert invited.role == ROLE_SECRETARY
    assert invited.professional_id is None
    assert invited.tenant_id == client.tenant_id
    assert invited.invite_token_hash is not None

    assert len(emails) == 1
    assert emails[0]["to"] == "nova@clinic.com"
    # Reused on purpose: an unknown template id is a SILENT no-send in secretaria.
    assert emails[0]["template"] == "professional_invite"
    assert emails[0]["variables"]["clinic_name"] == CLINIC

    # Duplicate email is a clean 409, same contract as the professional invite.
    dup = await client.post(
        "/doctor/secretaries/invites",
        json={"name": "Outra", "email": "nova@clinic.com"},
        headers=_bearer(token),
    )
    assert dup.status_code == 409
    assert dup.json()["detail"] == "email_already_registered"


async def test_secretary_can_invite_a_professional(client, monkeypatch):
    """Team management is not limited to inviting peers — a secretary invites doctors."""
    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _noop_async(True))
    monkeypatch.setattr(
        secretaria_provisioning,
        "create_professional",
        _noop_async({"professional_id": str(uuid4()), "created": True}),
    )
    token = await _token(client, SECRETARY_EMAIL, SECRETARY_PASSWORD)
    resp = await client.post(
        "/doctor/professionals/invites",
        json={"name": "Dr. Novo", "email": "novo@clinic.com"},
        headers=_bearer(token),
    )
    assert resp.status_code == 201, resp.text


async def test_invited_secretary_completes_the_invite_flow(client, monkeypatch):
    """End to end: invite -> redeem the single-use token -> set a password -> log in with
    the new role. `/auth/exchange-invite-token` is role-agnostic, which this proves."""
    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _noop_async(True))
    inviter = await _token(client, OWNER_EMAIL, OWNER_PASSWORD)
    resp = await client.post(
        "/doctor/secretaries/invites",
        json={"name": "Convidada", "email": "convidada@clinic.com"},
        headers=_bearer(inviter),
    )
    assert resp.status_code == 201, resp.text
    raw_token = resp.json()["invite_link"].split("token=")[1]

    exchanged = await client.post("/auth/exchange-invite-token", json={"token": raw_token})
    assert exchanged.status_code == 200, exchanged.text
    session_token = exchanged.json()["access_token"]
    assert decode_token(session_token)["role"] == ROLE_SECRETARY

    set_pw = await client.post(
        "/auth/set-password",
        json={"new_password": "novasenha1"},
        headers=_bearer(session_token),
    )
    assert set_pw.status_code == 204, set_pw.text

    # Single-use: the same token cannot be redeemed twice.
    replay = await client.post("/auth/exchange-invite-token", json={"token": raw_token})
    assert replay.status_code == 401

    fresh = await _token(client, "convidada@clinic.com", "novasenha1")
    assert decode_token(fresh)["role"] == ROLE_SECRETARY

    # The listing now reports her as active (invite burned), alongside the seeded one.
    listing = await client.get("/doctor/secretaries", headers=_bearer(inviter))
    assert listing.status_code == 200
    by_email = {row["email"]: row for row in listing.json()["items"]}
    assert by_email["convidada@clinic.com"]["invite_pending"] is False
    assert by_email[SECRETARY_EMAIL]["invite_pending"] is False


# --- REFUSED: the secretarIA-only boundary ---------------------------------


async def test_secretary_cannot_mint_a_precheck_sso_token(client):
    """The exclusion fires BEFORE the entitlement/link logic: the doctor on this same
    entitled tenant gets as far as 409 not-linked, the secretary never gets in at all."""
    secretary = await _token(client, SECRETARY_EMAIL, SECRETARY_PASSWORD)
    resp = await client.post("/sso/precheck/token", headers=_bearer(secretary))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "secretary_precheck_not_allowed"

    owner = await _token(client, OWNER_EMAIL, OWNER_PASSWORD)
    resp = await client.post("/sso/precheck/token", headers=_bearer(owner))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "precheck_account_not_linked"


async def test_secretary_cannot_read_anamneses(client):
    """Clinical data stays out of reach even though `require_doctor` admits the role."""
    secretary = await _token(client, SECRETARY_EMAIL, SECRETARY_PASSWORD)

    resp = await client.get("/doctor/anamneses", headers=_bearer(secretary))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "secretary_precheck_not_allowed"

    resp = await client.get("/doctor/anamneses/1", headers=_bearer(secretary))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "secretary_precheck_not_allowed"

    # Regression: a doctor still reaches the list (empty page, PreCheck unset in tests).
    owner = await _token(client, OWNER_EMAIL, OWNER_PASSWORD)
    resp = await client.get("/doctor/anamneses", headers=_bearer(owner))
    assert resp.status_code == 200


async def test_secretary_cannot_become_a_professional(client, monkeypatch):
    """The self-bind is the only route that writes `professional_id` — closed, so a
    receptionist can never appear in the bookable agenda."""

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("create_professional must NOT be reached")

    monkeypatch.setattr(secretaria_provisioning, "create_professional", must_not_be_called)

    token = await _token(client, SECRETARY_EMAIL, SECRETARY_PASSWORD)
    resp = await client.post("/doctor/professionals/self", json={}, headers=_bearer(token))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "secretary_cannot_be_professional"

    async with client.sessionmaker() as session:
        row = await session.scalar(select(User).where(User.email == SECRETARY_EMAIL))
    assert row.professional_id is None


async def test_secretary_is_not_a_platform_admin(client):
    """Unchanged by this round: `/admin/*` runs on `require_role("admin")`, which the new
    role does not satisfy."""
    token = await _token(client, SECRETARY_EMAIL, SECRETARY_PASSWORD)
    resp = await client.get("/admin/tenants", headers=_bearer(token))
    assert resp.status_code == 403


# --- Admin tooling ---------------------------------------------------------


async def test_admin_can_create_a_secretary(client):
    """The `Literal[...]` on `AdminUserCreateIn` accepts the new value, and the tenant
    requirement applies to it like any other tenant role."""
    admin = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    resp = await client.post(
        "/admin/users",
        json={
            "email": "admin.criada@clinic.com",
            "name": "Criada pelo Admin",
            "password": "criadapass1",
            "role": "secretary",
            "tenant_id": str(client.tenant_id),
        },
        headers=_bearer(admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == ROLE_SECRETARY
    # Nothing is force-set for a secretary (unlike `manager`, which forces is_manager).
    assert body["is_manager"] is False
    assert body["is_owner"] is False

    # A tenant role with no tenant is still a 422.
    resp = await client.post(
        "/admin/users",
        json={
            "email": "sem.tenant@clinic.com",
            "name": "Sem Tenant",
            "password": "sempass1234",
            "role": "secretary",
        },
        headers=_bearer(admin),
    )
    assert resp.status_code == 422


def _noop_async(result):
    """An async stand-in that ignores its arguments and returns `result`."""

    async def _fn(*args, **kwargs):
        return result

    return _fn
