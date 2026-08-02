"""POST /public/launch-waitlist — the pre-launch buy gate's lead capture.

Covers the contract the frontend's "Estamos quase lá" modal depends on: capture,
IDEMPOTENCY per email (the property that lets a visitor click three plans and stay one
row), the honeypot accept-and-drop, validation, the per-IP limiter, and the isolation
guarantee (no tenant / no entitlement ever created here).

Uses the shared in-memory `client` fixture (tests/conftest.py re-exports it from
test_rbac). `WAITLIST_RATE_LIMIT_PER_MIN` is 0 for the suite — the rate-limit test
installs its own limiter instead.
"""

from sqlalchemy import func, select

from brain_api.core.database import get_session
from brain_api.core.ratelimit import SlidingWindowLimiter
from brain_api.main import app
from brain_api.models import Entitlement, Tenant, WaitlistLead
from brain_api.services import waitlist as waitlist_service

ENDPOINT = "/public/launch-waitlist"


async def _db():
    """A session on the SAME in-memory DB the `client` fixture serves from."""
    gen = app.dependency_overrides[get_session]()
    return gen, await gen.__anext__()


async def _rows() -> list[WaitlistLead]:
    """Every captured lead, oldest first."""
    gen, session = await _db()
    try:
        result = await session.scalars(select(WaitlistLead).order_by(WaitlistLead.created_at))
        return list(result)
    finally:
        await gen.aclose()


async def test_capture_persists_lead(client):
    resp = await client.post(
        ENDPOINT,
        json={
            "name": "Dr. Aurélio Lima",
            "email": "voce@clinica.com.br",
            "plan_hint": "secretaria_basico",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"]
    assert body["message"]

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].name == "Dr. Aurélio Lima"
    assert rows[0].email == "voce@clinica.com.br"
    assert rows[0].plan_hint == "secretaria_basico"


async def test_email_is_stored_lowercased(client):
    resp = await client.post(
        ENDPOINT, json={"name": "Dra. Ana", "email": "Ana.Maiuscula@Clinica.COM.BR"}
    )
    assert resp.status_code == 201, resp.text
    rows = await _rows()
    assert [r.email for r in rows] == ["ana.maiuscula@clinica.com.br"]


async def test_resubmitting_same_email_is_idempotent(client):
    """The whole point: three plan clicks by one visitor leave ONE row, same id."""
    first = await client.post(
        ENDPOINT,
        json={"name": "Dr. Aurélio", "email": "repeat@clinica.com.br", "plan_hint": "precheck"},
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        ENDPOINT,
        json={
            "name": "Dr. Aurélio Lima",
            "email": "REPEAT@clinica.com.br",  # case variant must collapse too
            "plan_hint": "complete_clinic_combo",
        },
    )
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]

    rows = await _rows()
    assert len(rows) == 1
    # Latest click wins for the sales-facing fields...
    assert rows[0].name == "Dr. Aurélio Lima"
    assert rows[0].plan_hint == "complete_clinic_combo"


async def test_resubmitting_keeps_first_seen_created_at(client):
    """`created_at` means FIRST asked — a repeat submission must not rewrite it."""
    await client.post(ENDPOINT, json={"name": "Dr. A", "email": "keep@clinica.com.br"})
    original = (await _rows())[0].created_at

    await client.post(
        ENDPOINT, json={"name": "Dr. A", "email": "keep@clinica.com.br", "plan_hint": "precheck"}
    )
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].created_at == original
    assert rows[0].plan_hint == "precheck"


async def test_resubmitting_without_plan_hint_keeps_the_known_one(client):
    """A later hint-less click must not erase the plan we already know they wanted."""
    await client.post(
        ENDPOINT,
        json={"name": "Dr. A", "email": "hint@clinica.com.br", "plan_hint": "secretaria_basico"},
    )
    await client.post(ENDPOINT, json={"name": "Dr. A", "email": "hint@clinica.com.br"})

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].plan_hint == "secretaria_basico"


async def test_blank_plan_hint_stores_null(client):
    resp = await client.post(
        ENDPOINT, json={"name": "Dr. A", "email": "blank@clinica.com.br", "plan_hint": "   "}
    )
    assert resp.status_code == 201, resp.text
    assert (await _rows())[0].plan_hint is None


async def test_honeypot_drops_silently(client):
    """A filled honeypot returns 201 but persists nothing."""
    resp = await client.post(
        ENDPOINT,
        json={"name": "Spam Bot", "email": "bot@spam.com", "website": "http://spam.example"},
    )
    assert resp.status_code == 201
    assert await _rows() == []


async def test_missing_name_is_rejected(client):
    resp = await client.post(ENDPOINT, json={"email": "voce@clinica.com.br"})
    assert resp.status_code == 422


async def test_blank_name_is_rejected(client):
    resp = await client.post(ENDPOINT, json={"name": "   ", "email": "voce@clinica.com.br"})
    assert resp.status_code == 422


async def test_malformed_email_is_rejected(client):
    resp = await client.post(ENDPOINT, json={"name": "Dr. A", "email": "not-an-email"})
    assert resp.status_code == 422
    assert await _rows() == []


async def test_rate_limit_trips(client, monkeypatch):
    """Second call from the same IP inside the window -> 429 (limit forced to 1)."""
    monkeypatch.setattr(
        waitlist_service, "_limiter", SlidingWindowLimiter("waitlist-test", lambda: 1)
    )
    first = await client.post(ENDPOINT, json={"name": "Dr. A", "email": "one@clinica.com.br"})
    assert first.status_code == 201, first.text

    second = await client.post(ENDPOINT, json={"name": "Dr. B", "email": "two@clinica.com.br"})
    assert second.status_code == 429


async def test_capture_creates_no_tenant_or_entitlement(client):
    """Isolated lead capture (CONTRACTS.md §0.4): no tenant, no entitlement, no billing.

    The `client` fixture seeds its own tenants, so this asserts the counts are UNCHANGED
    by the waitlist POST rather than zero.
    """
    gen, session = await _db()
    try:
        tenants_before = await session.scalar(select(func.count()).select_from(Tenant))
        ents_before = await session.scalar(select(func.count()).select_from(Entitlement))
    finally:
        await gen.aclose()

    resp = await client.post(ENDPOINT, json={"name": "Dr. A", "email": "iso@clinica.com.br"})
    assert resp.status_code == 201, resp.text

    gen, session = await _db()
    try:
        assert await session.scalar(select(func.count()).select_from(Tenant)) == tenants_before
        assert await session.scalar(select(func.count()).select_from(Entitlement)) == ents_before
    finally:
        await gen.aclose()
