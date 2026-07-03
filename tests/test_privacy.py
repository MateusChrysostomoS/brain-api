"""LGPD cross-database privacy orchestration tests (CONTRACTS.md §14).

Ground truth: services/privacy.py (subject normalization, local brain erasure/export,
upstream fan-out) and api/privacy.py (admin gate, confirm guard). Both upstreams
(secretaria, precheck) are mocked via the repo's fake-httpx pattern (mirrors
tests/test_doctor_secretaria.py): the httpx layer inside services/privacy is
monkeypatched, so these exercise the REAL client/orchestration logic with no network.

Uses its own seeded in-memory app (`privacy_env`, not test_rbac's `client` fixture) so
tests can query the persisted `privacy_requests` audit row directly via the same
sessionmaker the app is wired to. The role-gate test reuses the RBAC-seeded `client`
fixture (provided globally via conftest's re-export — no import needed).
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from brain_api.core.database import Base, get_session
from brain_api.core.security import hash_password
from brain_api.main import app
from brain_api.models import DemoRequest, PrivacyRequest, Tenant, User
from brain_api.models.user import ROLE_ADMIN, ROLE_TENANT_OWNER
from brain_api.services import privacy as privacy_service
from tests.test_rbac import OWNER_A_EMAIL, OWNER_A_PASSWORD, _bearer, _token

ADMIN_EMAIL = "privacy.admin@brain.co"
ADMIN_PASSWORD = "privacyadmin1"
DOCTOR_EMAIL = "privacy.doctor@clinic.com"
DOCTOR_PASSWORD = "privacydoc1"
CLINIC_NAME = "Clínica Privacy"
WA_ID = "5511999999999"
PATIENT_EMAIL = "privacy.patient@example.com"

SECRETARIA_BASE = "http://secretaria:8000"
PRECHECK_BASE = "http://precheck:8000"

ERASURE = "/admin/privacy/erasure"
EXPORT = "/admin/privacy/export"


@pytest_asyncio.fixture
async def privacy_env():
    """A dedicated seeded app: a platform admin, a tenant + doctor user (erasable),
    and two demo_requests leads (one for the doctor's own email, one for a "patient"
    email) — everything services/privacy.py's brain leg touches."""
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
                name="Privacy Admin",
                password_hash=hash_password(ADMIN_PASSWORD),
                role=ROLE_ADMIN,
            )
        )
        tenant = Tenant(clinic_name=CLINIC_NAME)
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
        session.add(
            User(
                tenant_id=tenant_id,
                email=DOCTOR_EMAIL,
                name="Dr. Privacy",
                password_hash=hash_password(DOCTOR_PASSWORD),
                role=ROLE_TENANT_OWNER,
            )
        )
        session.add(DemoRequest(name="Doctor Lead", email=DOCTOR_EMAIL))
        session.add(DemoRequest(name="Patient Lead", email=PATIENT_EMAIL))

    async def _override_get_session():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield SimpleNamespace(client=c, sessionmaker=sessionmaker, tenant_id=tenant_id)
    app.dependency_overrides.clear()
    await engine.dispose()


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


def _install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    secretaria: _FakeResponse | Exception | None = None,
    precheck: _FakeResponse | Exception | None = None,
) -> list[dict]:
    """Point services/privacy at a configured mesh + a fake httpx routed by base_url.

    A leg left as `None` stays UNCONFIGURED (empty base URL/key/token) — its httpx
    client is never constructed, matching the real `skipped_unconfigured` path.
    """
    calls: list[dict] = []

    def _resolve(base_url: str) -> _FakeResponse | Exception:
        if base_url == SECRETARIA_BASE:
            assert secretaria is not None
            return secretaria
        if base_url == PRECHECK_BASE:
            assert precheck is not None
            return precheck
        raise AssertionError(f"unexpected base_url {base_url!r}")

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            self._base_url = kwargs.get("base_url")

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def request(self, method: str, path: str, headers=None, params=None):
            calls.append(
                {"base_url": self._base_url, "method": method, "path": path, "headers": headers}
            )
            outcome = _resolve(self._base_url)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        async def post(self, path: str, headers=None, json=None):
            calls.append(
                {
                    "base_url": self._base_url,
                    "method": "POST",
                    "path": path,
                    "headers": headers,
                    "json": json,
                }
            )
            outcome = _resolve(self._base_url)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(privacy_service.httpx, "AsyncClient", _FakeClient)

    settings = SimpleNamespace(
        SECRETARIA_BASE_URL=SECRETARIA_BASE if secretaria is not None else "",
        SECRETARIA_API_KEY="sec-key" if secretaria is not None else "",
        SECRETARIA_TIMEOUT_SECONDS=10.0,
        PRECHECK_BASE_URL=PRECHECK_BASE if precheck is not None else "",
        PRECHECK_INTERNAL_TOKEN="pre-token" if precheck is not None else "",
        PRECHECK_TIMEOUT_SECONDS=10.0,
    )
    monkeypatch.setattr(privacy_service, "get_settings", lambda: settings)
    return calls


async def _admin_token(client) -> str:
    return await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)


async def _privacy_requests(sessionmaker) -> list[PrivacyRequest]:
    async with sessionmaker() as session:
        return list((await session.scalars(select(PrivacyRequest))).all())


# --- Role gate + validation ---------------------------------------------------------


@pytest.mark.parametrize("route", [ERASURE, EXPORT])
async def test_privacy_requires_admin(client, route: str) -> None:
    """Non-admin -> 403 (with a VALID body, so the 403 is really the role gate firing
    before the handler); no token -> 401. Reuses the RBAC-seeded `client` fixture."""
    owner_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    body = {"subject_type": "user", "email": OWNER_A_EMAIL, "confirm": True}
    resp = await client.post(route, headers=_bearer(owner_token), json=body)
    assert resp.status_code == 403, resp.text
    resp_noauth = await client.post(route, json=body)
    assert resp_noauth.status_code == 401


async def test_erasure_confirm_false_400(privacy_env) -> None:
    token = await _admin_token(privacy_env.client)
    resp = await privacy_env.client.post(
        ERASURE,
        headers=_bearer(token),
        json={"subject_type": "user", "email": DOCTOR_EMAIL, "confirm": False},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize(
    "body",
    [
        {"subject_type": "user", "confirm": True},
        {"subject_type": "patient", "confirm": True},
        {"subject_type": "patient", "wa_id": WA_ID, "confirm": True},
    ],
)
async def test_erasure_bad_subject_combo_422(privacy_env, body: dict) -> None:
    """user needs email; patient needs BOTH wa_id and tenant_id."""
    token = await _admin_token(privacy_env.client)
    resp = await privacy_env.client.post(ERASURE, headers=_bearer(token), json=body)
    assert resp.status_code == 422, resp.text


# --- Erasure: happy path, all three legs --------------------------------------------


async def test_erasure_happy_path_all_three_legs(
    privacy_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    secretaria_body = {
        "erased": {"patients": 1, "conversations": 4, "messages": 37, "appointments": 2}
    }
    precheck_body = {"erased": {"anamneses": 1}}
    _install_fake_httpx(
        monkeypatch,
        secretaria=_FakeResponse(200, secretaria_body),
        precheck=_FakeResponse(200, precheck_body),
    )

    token = await _admin_token(privacy_env.client)
    resp = await privacy_env.client.post(
        ERASURE,
        headers=_bearer(token),
        json={
            "subject_type": "patient",
            "wa_id": WA_ID,
            "tenant_id": str(privacy_env.tenant_id),
            "email": PATIENT_EMAIL,
            "confirm": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["brain"] == {"erased": {"demo_requests": 1}}
    assert body["secretaria"] == secretaria_body
    assert body["precheck"] == precheck_body
    request_id = body["request_id"]

    rows = await _privacy_requests(privacy_env.sessionmaker)
    assert len(rows) == 1
    row = rows[0]
    assert str(row.id) == request_id
    assert row.kind == "erasure"
    assert row.subject_type == "patient"
    assert row.status == "completed"
    assert row.result == {
        "brain": body["brain"],
        "secretaria": secretaria_body,
        "precheck": precheck_body,
    }
    expected_key = privacy_service._normalize_subject_key(
        "patient", email=PATIENT_EMAIL, wa_id=WA_ID, tenant_id=privacy_env.tenant_id
    )
    assert row.subject_hash == privacy_service.subject_hash(expected_key)


async def test_erasure_idempotent_second_run(privacy_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second erasure of the same subject finds nothing left locally, and reports
    zero counts from the (mocked) upstreams too — still `completed`."""
    _install_fake_httpx(
        monkeypatch,
        secretaria=_FakeResponse(200, {"erased": {"patients": 1}}),
        precheck=_FakeResponse(200, {"erased": {"anamneses": 1}}),
    )
    token = await _admin_token(privacy_env.client)
    payload = {
        "subject_type": "patient",
        "wa_id": WA_ID,
        "tenant_id": str(privacy_env.tenant_id),
        "email": PATIENT_EMAIL,
        "confirm": True,
    }
    first = await privacy_env.client.post(ERASURE, headers=_bearer(token), json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["brain"] == {"erased": {"demo_requests": 1}}

    # Re-point the mesh at fakes that now report zero (the upstream's own idempotency).
    _install_fake_httpx(
        monkeypatch,
        secretaria=_FakeResponse(200, {"erased": {"patients": 0}}),
        precheck=_FakeResponse(200, {"erased": {"anamneses": 0}}),
    )
    second = await privacy_env.client.post(ERASURE, headers=_bearer(token), json=payload)
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["status"] == "completed"
    assert second_body["brain"] == {"erased": {"demo_requests": 0}}
    assert second_body["secretaria"] == {"erased": {"patients": 0}}
    assert second_body["precheck"] == {"erased": {"anamneses": 0}}

    rows = await _privacy_requests(privacy_env.sessionmaker)
    assert len(rows) == 2  # one audit row per invocation, even when nothing was left


async def test_erasure_upstream_failure_partial(
    privacy_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_httpx(
        monkeypatch,
        secretaria=_FakeResponse(500, {"detail": "boom"}),
        precheck=_FakeResponse(200, {"erased": {"anamneses": 1}}),
    )
    token = await _admin_token(privacy_env.client)
    resp = await privacy_env.client.post(
        ERASURE,
        headers=_bearer(token),
        json={
            "subject_type": "patient",
            "wa_id": WA_ID,
            "tenant_id": str(privacy_env.tenant_id),
            "confirm": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "partial"
    assert body["secretaria"] == {"status": "failed"}
    assert body["precheck"] == {"erased": {"anamneses": 1}}

    rows = await _privacy_requests(privacy_env.sessionmaker)
    assert rows[-1].status == "partial"


async def test_erasure_unconfigured_upstreams_partial(privacy_env) -> None:
    """No fake httpx installed and no settings monkeypatch: the real (hermetic test-env)
    mesh is unconfigured, so both legs degrade to skipped_unconfigured, no network call."""
    token = await _admin_token(privacy_env.client)
    resp = await privacy_env.client.post(
        ERASURE,
        headers=_bearer(token),
        json={
            "subject_type": "patient",
            "wa_id": WA_ID,
            "tenant_id": str(privacy_env.tenant_id),
            "confirm": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "partial"
    assert body["secretaria"] == {"status": "skipped_unconfigured"}
    assert body["precheck"] == {"status": "skipped_unconfigured"}


async def test_erasure_admin_target_refused_409(privacy_env) -> None:
    token = await _admin_token(privacy_env.client)
    resp = await privacy_env.client.post(
        ERASURE,
        headers=_bearer(token),
        json={"subject_type": "user", "email": ADMIN_EMAIL, "confirm": True},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "admin_cannot_be_erased"

    # No audit row is written for a refused erasure.
    rows = await _privacy_requests(privacy_env.sessionmaker)
    assert rows == []


# --- Export ---------------------------------------------------------------------------


async def test_export_happy_path_aggregates_bundle_and_audits_counts(
    privacy_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    secretaria_bundle = {"conversations": [{"id": "c1"}, {"id": "c2"}], "patient": {"id": "p1"}}
    precheck_bundle = {"anamneses": [{"id": 1}]}
    _install_fake_httpx(
        monkeypatch,
        secretaria=_FakeResponse(200, secretaria_bundle),
        precheck=_FakeResponse(200, precheck_bundle),
    )
    token = await _admin_token(privacy_env.client)
    resp = await privacy_env.client.post(
        EXPORT,
        headers=_bearer(token),
        json={
            "subject_type": "patient",
            "wa_id": WA_ID,
            "tenant_id": str(privacy_env.tenant_id),
            "email": PATIENT_EMAIL,
            "confirm": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["secretaria"] == secretaria_bundle
    assert body["precheck"] == precheck_bundle
    assert len(body["brain"]["demo_requests"]) == 1
    assert body["brain"]["demo_requests"][0]["email"] == PATIENT_EMAIL

    rows = await _privacy_requests(privacy_env.sessionmaker)
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "export"
    assert row.status == "completed"
    # COUNTS ONLY — never the collected data itself.
    assert row.result == {
        "brain": {"demo_requests": 1},
        "secretaria": {"conversations": 2, "patient": 1},
        "precheck": {"anamneses": 1},
    }


async def test_export_user_no_password_hash_leak(privacy_env) -> None:
    """A user export: secretaria is not_applicable (no wa_id -> not a config problem);
    precheck degrades to skipped_unconfigured (hermetic test env). No password_hash
    anywhere in the response, and brain's user contribution is a tight whitelist."""
    token = await _admin_token(privacy_env.client)
    resp = await privacy_env.client.post(
        EXPORT,
        headers=_bearer(token),
        json={"subject_type": "user", "email": DOCTOR_EMAIL, "confirm": True},
    )
    assert resp.status_code == 200, resp.text
    assert "password_hash" not in resp.text
    body = resp.json()
    assert body["secretaria"] == {"status": "not_applicable"}
    assert body["precheck"] == {"status": "skipped_unconfigured"}
    assert body["brain"]["user"]["email"] == DOCTOR_EMAIL
    assert set(body["brain"]["user"].keys()) == {"id", "email", "name", "role", "created_at"}


# --- Non-leak: the persisted audit row never carries a raw identifier ----------------


async def test_privacy_request_row_never_leaks_raw_identifier(
    privacy_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_httpx(
        monkeypatch,
        secretaria=_FakeResponse(200, {"erased": {"patients": 1}}),
        precheck=_FakeResponse(200, {"erased": {"anamneses": 1}}),
    )
    token = await _admin_token(privacy_env.client)
    resp = await privacy_env.client.post(
        ERASURE,
        headers=_bearer(token),
        json={
            "subject_type": "patient",
            "wa_id": WA_ID,
            "tenant_id": str(privacy_env.tenant_id),
            "email": PATIENT_EMAIL,
            "confirm": True,
        },
    )
    assert resp.status_code == 200, resp.text

    rows = await _privacy_requests(privacy_env.sessionmaker)
    assert len(rows) == 1
    row = rows[0]
    raw_columns = str({c.name: getattr(row, c.name) for c in row.__table__.columns})
    assert WA_ID not in raw_columns
    assert PATIENT_EMAIL not in raw_columns
    # subject_hash is present and is exactly the sha256 of the normalized key.
    expected_key = privacy_service._normalize_subject_key(
        "patient", email=PATIENT_EMAIL, wa_id=WA_ID, tenant_id=privacy_env.tenant_id
    )
    assert row.subject_hash == privacy_service.subject_hash(expected_key)
