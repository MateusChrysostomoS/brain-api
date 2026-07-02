"""RBAC enforcement tests (RBAC task — Testing requirements).

Covers the three required cases plus the role-gate matrix:
- `test_admin_role_required`: every /admin/* route returns 403 for a non-admin JWT.
- `test_tenant_isolation`: a tenant_owner of tenant A cannot reach tenant B's data.
- `test_admin_seed_idempotent`: running the admin seed twice creates no duplicate / error.

Runs the real FastAPI app against in-memory aiosqlite (no Postgres). PRECHECK_BASE_URL is
unset in tests, so the precheck proxy routes return an empty page (no network).
"""

import importlib.util
import pathlib

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from brain_api.core.database import Base, get_session
from brain_api.core.security import hash_password
from brain_api.main import app
from brain_api.models import DemoRequest, Entitlement, Tenant, User
from brain_api.models.user import ROLE_ADMIN, ROLE_TENANT_OWNER

# `scripts/` is not part of the installed wheel, so load seed_admin from its file path.
_SEED_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "seed_admin.py"
_spec = importlib.util.spec_from_file_location("seed_admin", _SEED_PATH)
_seed_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_seed_mod)
seed_admin = _seed_mod.seed_admin

ADMIN_EMAIL = "admin@brain.co"
ADMIN_PASSWORD = "adminpass1"
OWNER_A_EMAIL = "ownera@a.com"
OWNER_A_PASSWORD = "ownerapass1"
CLINIC_A = "Clínica A"
OWNER_B_EMAIL = "ownerb@b.com"
OWNER_B_PASSWORD = "ownerbpass1"
CLINIC_B = "Clínica B"

# A fixed, non-existent UUID: the router-level role gate fires before the handler, so a
# non-admin hits 403 regardless of whether the id exists.
MISSING_ID = "00000000-0000-0000-0000-000000000000"

# The admin-only routes (the GET surface) used by the role-required matrix.
ADMIN_GET_ROUTES = [
    "/admin/tenants",
    "/admin/users",
    "/admin/demo_requests",
    "/admin/inbound",
    f"/admin/tenants/{MISSING_ID}",
    f"/admin/tenants/{MISSING_ID}/entitlements",
]

DOCTOR_GET_ROUTES = [
    "/doctor/me",
    "/doctor/appointments",
    "/doctor/patients",
    "/doctor/anamneses",
]


@pytest_asyncio.fixture
async def client():
    """Test client over in-memory SQLite, seeded with an admin + two tenants."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with sessionmaker() as session, session.begin():
        # Platform admin (no tenant).
        session.add(
            User(
                tenant_id=None,
                email=ADMIN_EMAIL,
                name="Brain Co Admin",
                password_hash=hash_password(ADMIN_PASSWORD),
                role=ROLE_ADMIN,
            )
        )
        # Tenant A — entitled to PreCheck only.
        t_a = Tenant(clinic_name=CLINIC_A)
        session.add(t_a)
        await session.flush()
        session.add(
            User(
                tenant_id=t_a.id,
                email=OWNER_A_EMAIL,
                name="Owner A",
                password_hash=hash_password(OWNER_A_PASSWORD),
                role=ROLE_TENANT_OWNER,
            )
        )
        session.add(
            Entitlement(
                tenant_id=t_a.id,
                precheck_enabled=True,
                secretaria_enabled=False,
                plan="precheck",
                status="active",
            )
        )
        # Tenant B — no entitlement row (default resolution).
        t_b = Tenant(clinic_name=CLINIC_B)
        session.add(t_b)
        await session.flush()
        session.add(
            User(
                tenant_id=t_b.id,
                email=OWNER_B_EMAIL,
                name="Owner B",
                password_hash=hash_password(OWNER_B_PASSWORD),
                role=ROLE_TENANT_OWNER,
            )
        )
        # One inbound lead for the demo-request PATCH test.
        session.add(DemoRequest(name="Lead Teste", email="lead@x.com"))

    async def _override_get_session():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


async def _token(client, email, password) -> str:
    resp = await client.post("/auth/token", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- Required: admin role gate ---------------------------------------------


async def test_admin_role_required(client):
    """Every /admin/* route is 403 for a non-admin token, 401 without a token."""
    owner_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)

    for route in ADMIN_GET_ROUTES:
        resp = await client.get(route, headers=_bearer(owner_token))
        assert resp.status_code == 403, f"{route} should be 403 for non-admin: {resp.text}"
        resp_noauth = await client.get(route)
        assert resp_noauth.status_code == 401, f"{route} should be 401 without token"

    # Mutating admin routes are gated too (gate runs before the handler).
    assert (
        await client.post(
            "/admin/users",
            headers=_bearer(owner_token),
            json={
                "email": "x@y.com",
                "name": "X",
                "password": "pw123456",
                "role": "tenant_staff",
                "tenant_id": MISSING_ID,
            },
        )
    ).status_code == 403
    assert (
        await client.patch(
            f"/admin/tenants/{MISSING_ID}/entitlements",
            headers=_bearer(owner_token),
            json={"precheck_enabled": True},
        )
    ).status_code == 403
    assert (
        await client.patch(
            f"/admin/demo_requests/{MISSING_ID}",
            headers=_bearer(owner_token),
            json={"status": "contacted"},
        )
    ).status_code == 403


async def test_admin_token_passes_admin_routes(client):
    """Sanity: an admin token is accepted (not 403) on the admin surface."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    for route in ["/admin/tenants", "/admin/users", "/admin/demo_requests"]:
        resp = await client.get(route, headers=_bearer(admin_token))
        assert resp.status_code == 200, f"{route}: {resp.text}"
        body = resp.json()
        assert "items" in body and "total" in body


# --- Required: tenant isolation --------------------------------------------


async def test_tenant_isolation(client):
    """A tenant_owner of A cannot read tenant B's data; their token resolves only to A."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    owner_a_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)

    # Discover tenant B's id via the admin listing.
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    tenant_b_id = next(t["id"] for t in tenants if t["clinic_name"] == CLINIC_B)

    # Owner A cannot use the (admin-only) tenant-detail route to peek at tenant B.
    resp = await client.get(f"/admin/tenants/{tenant_b_id}", headers=_bearer(owner_a_token))
    assert resp.status_code == 403

    # Owner A's own scoped views resolve ONLY to tenant A (never B), purely from the JWT.
    me = (await client.get("/doctor/me", headers=_bearer(owner_a_token))).json()
    assert me["tenant"]["clinic_name"] == CLINIC_A
    assert me["tenant"]["id"] != tenant_b_id

    ent = (await client.get("/entitlements", headers=_bearer(owner_a_token))).json()
    assert ent["clinic_name"] == CLINIC_A


async def test_doctor_routes_reject_admin(client):
    """All /doctor/* routes return 403 for an admin token (wrong portal)."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    for route in DOCTOR_GET_ROUTES:
        resp = await client.get(route, headers=_bearer(admin_token))
        assert resp.status_code == 403, f"{route} should reject admin: {resp.text}"


# --- Required: idempotent admin seed ---------------------------------------


async def test_admin_seed_idempotent():
    """Running the admin seed twice creates exactly one admin and never errors."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    created_first = await seed_admin(
        email="seed.admin@brain.co", password="seedpass1", session_factory=sm
    )
    created_second = await seed_admin(
        email="seed.admin@brain.co", password="seedpass1", session_factory=sm
    )

    assert created_first is True
    assert created_second is False  # second run is a no-op, not an error

    async with sm() as session:
        count = await session.scalar(
            select(func.count()).select_from(User).where(User.email == "seed.admin@brain.co")
        )
        assert count == 1
        admin = await session.scalar(select(User).where(User.email == "seed.admin@brain.co"))
        assert admin.role == ROLE_ADMIN
        assert admin.tenant_id is None  # platform-level, not tenant-scoped

    await engine.dispose()


# --- Admin CRUD behaviour ---------------------------------------------------


async def test_admin_create_user_validation(client):
    """Create-user enforces role/tenant rules, email uniqueness, and password length."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    tenant_a_id = next(t["id"] for t in tenants if t["clinic_name"] == CLINIC_A)

    # Happy path: create a tenant_staff in tenant A.
    resp = await client.post(
        "/admin/users",
        headers=_bearer(admin_token),
        json={
            "email": "staff@a.com",
            "name": "Staff A",
            "password": "staffpass1",
            "role": "tenant_staff",
            "tenant_id": tenant_a_id,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == "tenant_staff"
    assert body["tenant_id"] == tenant_a_id
    assert "password_hash" not in body and "password_hash" not in str(body)

    # The new user can authenticate.
    assert (
        await client.post("/auth/token", json={"email": "staff@a.com", "password": "staffpass1"})
    ).status_code == 200

    # Tenant role without tenant_id -> 422.
    assert (
        await client.post(
            "/admin/users",
            headers=_bearer(admin_token),
            json={"email": "n1@a.com", "name": "N", "password": "pw123456", "role": "tenant_owner"},
        )
    ).status_code == 422

    # Admin role WITH a tenant_id -> 422.
    assert (
        await client.post(
            "/admin/users",
            headers=_bearer(admin_token),
            json={
                "email": "n2@a.com",
                "name": "N",
                "password": "pw123456",
                "role": "admin",
                "tenant_id": tenant_a_id,
            },
        )
    ).status_code == 422

    # Duplicate email -> 409.
    assert (
        await client.post(
            "/admin/users",
            headers=_bearer(admin_token),
            json={
                "email": "staff@a.com",
                "name": "Dup",
                "password": "pw123456",
                "role": "tenant_staff",
                "tenant_id": tenant_a_id,
            },
        )
    ).status_code == 409


async def test_admin_users_listing_never_leaks_hash(client):
    """The users listing must never serialize password_hash."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    body = (await client.get("/admin/users", headers=_bearer(admin_token))).json()
    assert "password_hash" not in str(body)
    # Admin row shows no clinic (Platform Admin); tenant rows show their clinic.
    by_email = {u["email"]: u for u in body["items"]}
    assert by_email[ADMIN_EMAIL]["clinic_name"] is None
    assert by_email[OWNER_A_EMAIL]["clinic_name"] == CLINIC_A


async def test_admin_entitlement_patch_toggles_products(client):
    """PATCH entitlements flips a product flag and is reflected on the read."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    tenant_a_id = next(t["id"] for t in tenants if t["clinic_name"] == CLINIC_A)

    resp = await client.patch(
        f"/admin/tenants/{tenant_a_id}/entitlements",
        headers=_bearer(admin_token),
        json={"secretaria_enabled": True, "plan": "brain-completo"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["secretaria_enabled"] is True
    assert body["precheck_enabled"] is True  # untouched field preserved
    assert body["plan"] == "brain-completo"

    # Persisted: a fresh read reflects it, and never leaks an _encrypted field.
    read = (
        await client.get(f"/admin/tenants/{tenant_a_id}/entitlements", headers=_bearer(admin_token))
    ).json()
    assert read["secretaria_enabled"] is True
    assert "_encrypted" not in str(read)


async def test_admin_entitlement_patch_creates_row_for_tenant_without_one(client):
    """A tenant with no entitlement row gets one created by PATCH (upsert)."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    tenant_b_id = next(t["id"] for t in tenants if t["clinic_name"] == CLINIC_B)

    resp = await client.patch(
        f"/admin/tenants/{tenant_b_id}/entitlements",
        headers=_bearer(admin_token),
        json={"precheck_enabled": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["precheck_enabled"] is True


async def test_admin_tenant_detail_404(client):
    """Tenant detail 404s for an unknown id (admin token, so past the gate)."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    resp = await client.get(f"/admin/tenants/{MISSING_ID}", headers=_bearer(admin_token))
    assert resp.status_code == 404


async def test_admin_demo_request_patch(client):
    """Demo-request PATCH moves status; an out-of-set value is rejected (422)."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    leads = (await client.get("/admin/demo_requests", headers=_bearer(admin_token))).json()
    lead_id = leads["items"][0]["id"]

    resp = await client.patch(
        f"/admin/demo_requests/{lead_id}",
        headers=_bearer(admin_token),
        json={"status": "contacted"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "contacted"

    # "new" is the initial state, not a valid PATCH target.
    assert (
        await client.patch(
            f"/admin/demo_requests/{lead_id}",
            headers=_bearer(admin_token),
            json={"status": "new"},
        )
    ).status_code == 422


# --- Doctor routes ----------------------------------------------------------


async def test_doctor_me_scoped_and_no_secrets(client):
    """/doctor/me returns the caller's own tenant + entitlements, no secrets."""
    owner_a_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/doctor/me", headers=_bearer(owner_a_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["email"] == OWNER_A_EMAIL
    assert body["tenant"]["clinic_name"] == CLINIC_A
    assert body["entitlements"]["products"] == {"precheck": True, "secretaria": False}
    assert "password_hash" not in str(body)


async def test_doctor_appointments_patients_empty_when_secretaria_unconfigured(client):
    """appointments/patients sit behind the doctor gate and fail closed to an empty page.

    With no SECRETARIA_BASE_URL / INTERNAL_API_KEY in the test env, the secretaria internal
    call is skipped (no network) and returns a safe empty page — never a 500. The configured
    path (passthrough, tenant scoping, 502 mapping) is covered in test_doctor_secretaria.py.
    """
    owner_a_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    for route in ["/doctor/appointments", "/doctor/patients"]:
        resp = await client.get(route, headers=_bearer(owner_a_token))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"data": [], "stub": True}


async def test_doctor_anamneses_empty_when_precheck_unconfigured(client):
    """With no PRECHECK_BASE_URL, the anamneses proxy returns an empty page (no network)."""
    owner_a_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/doctor/anamneses", headers=_bearer(owner_a_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["stub"] is True


async def test_admin_inbound_empty_when_precheck_unconfigured(client):
    """With no PRECHECK_BASE_URL, the admin inbound proxy returns an empty page."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    resp = await client.get("/admin/inbound", headers=_bearer(admin_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []
