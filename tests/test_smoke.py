"""End-to-end smoke test of the full HTTP contract (CONTRACTS.md).

Runs the real FastAPI app against an in-memory aiosqlite database (no Postgres /
Docker needed). Exercises auth, entitlements (both the entitled and the
default-resolution paths), and demo-request capture (happy path, validation,
honeypot). Asserts the never-leak rule (no password_hash in /auth/me).
"""

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from brain_api.core.database import Base, get_session
from brain_api.core.security import hash_password
from brain_api.main import app
from brain_api.models import Entitlement, Tenant, User
from brain_api.models.user import ROLE_TENANT_OWNER

# Environment for settings is configured in tests/conftest.py (loaded first).

OWNER_EMAIL = "dra.demo@clinica.com.br"
OWNER_PASSWORD = "demo1234"
OWNER_CLINIC = "Consultório Dr. Aurélio Lima"

# A second tenant WITHOUT an entitlement row, to exercise default resolution.
PLAIN_EMAIL = "sem.plano@clinica.com.br"
PLAIN_PASSWORD = "semplano1"


@pytest_asyncio.fixture
async def client():
    """A test client backed by a shared in-memory SQLite database."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed: entitled tenant + owner, and a second tenant with no entitlement row.
    async with sessionmaker() as session, session.begin():
        t1 = Tenant(clinic_name=OWNER_CLINIC)
        session.add(t1)
        await session.flush()
        session.add(
            User(
                tenant_id=t1.id,
                email=OWNER_EMAIL,
                name="Dra. Demo",
                password_hash=hash_password(OWNER_PASSWORD),
                role=ROLE_TENANT_OWNER,
            )
        )
        session.add(
            Entitlement(
                tenant_id=t1.id,
                precheck_enabled=True,
                secretaria_enabled=True,
                plan="brain-completo",
                status="active",
            )
        )

        t2 = Tenant(clinic_name="Clínica Sem Plano")
        session.add(t2)
        await session.flush()
        session.add(
            User(
                tenant_id=t2.id,
                email=PLAIN_EMAIL,
                name="Sem Plano",
                password_hash=hash_password(PLAIN_PASSWORD),
                role=ROLE_TENANT_OWNER,
            )
        )

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


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_login_success(client):
    resp = await client.post("/auth/token", json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"


async def test_login_bad_password(client):
    resp = await client.post(
        "/auth/token", json={"email": OWNER_EMAIL, "password": "wrong-password"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Credenciais inválidas"


async def test_login_unknown_email(client):
    resp = await client.post(
        "/auth/token", json={"email": "nobody@nowhere.com", "password": "whatever1"}
    )
    assert resp.status_code == 401


async def test_login_password_too_long(client):
    resp = await client.post("/auth/token", json={"email": OWNER_EMAIL, "password": "x" * 73})
    assert resp.status_code == 422


async def test_me_identity_only(client):
    token = await _token(client, OWNER_EMAIL, OWNER_PASSWORD)
    resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["email"] == OWNER_EMAIL
    assert body["user"]["role"] == ROLE_TENANT_OWNER
    assert body["tenant"]["clinic_name"] == OWNER_CLINIC
    # Never leak the password hash (tenant-secrets-encryption rule).
    assert "password_hash" not in body["user"]
    assert "password_hash" not in str(body)


async def test_me_requires_token(client):
    resp = await client.get("/auth/me")
    assert resp.status_code == 401


async def test_entitlements_entitled_tenant(client):
    token = await _token(client, OWNER_EMAIL, OWNER_PASSWORD)
    resp = await client.get("/entitlements", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["products"] == {"precheck": True, "secretaria": True}
    assert body["plan"] == "brain-completo"
    assert body["status"] == "active"
    assert body["clinic_name"] == OWNER_CLINIC


async def test_entitlements_default_when_missing(client):
    """A valid tenant with no entitlement row gets coherent defaults, not a 404."""
    token = await _token(client, PLAIN_EMAIL, PLAIN_PASSWORD)
    resp = await client.get("/entitlements", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["products"] == {"precheck": False, "secretaria": False}
    assert body["plan"] == "free"
    assert body["status"] == "inactive"


async def test_entitlements_requires_token(client):
    resp = await client.get("/entitlements")
    assert resp.status_code == 401


async def test_demo_request_happy_path(client):
    resp = await client.post(
        "/demo-requests",
        json={
            "name": "Dr. Aurélio Lima",
            "email": "voce@clinica.com.br",
            "clinic": "Consultório Dr. Aurélio Lima",
            "profile": "clinica_privada",
            "product_interest": "ambos",
            "message": "Quero ver retornos.",
            "source": "brain",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"]
    assert body["status"] == "new"
    assert body["message"]


async def test_demo_request_missing_name(client):
    resp = await client.post("/demo-requests", json={"email": "voce@clinica.com.br"})
    assert resp.status_code == 422


async def test_demo_request_bad_enum(client):
    resp = await client.post(
        "/demo-requests",
        json={"name": "X", "email": "voce@clinica.com.br", "profile": "not_a_profile"},
    )
    assert resp.status_code == 422


async def test_demo_request_honeypot_drops_silently(client):
    """A filled honeypot returns 201 but persists nothing."""
    resp = await client.post(
        "/demo-requests",
        json={
            "name": "Spam Bot",
            "email": "bot@spam.com",
            "website": "http://spam.example",
        },
    )
    assert resp.status_code == 201
    # The bot's submission must not have created a row: only the happy-path test
    # ever persists, and each test gets a fresh DB, so the count here is 0.
    from sqlalchemy import func, select

    from brain_api.models import DemoRequest

    # Reuse the override's sessionmaker via a fresh request would be circular;
    # instead assert through the public API is not possible, so check via a direct
    # query on a new session bound to the same app override.
    gen = app.dependency_overrides[get_session]()
    session = await gen.__anext__()
    try:
        count = await session.scalar(select(func.count()).select_from(DemoRequest))
        assert count == 0
    finally:
        await gen.aclose()
