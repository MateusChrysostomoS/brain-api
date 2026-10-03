from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from brain_api.models.patient_access import (
    MessagePatient,
    MessagePatientAccount,
    MessagePatientAccountOtp,
    MessagePatientSession,
    MessagePendingSession,
)
from brain_api.models.tenant import Tenant
from brain_api.schemas.tenant_patient_cleanup import CleanupRequest, ProductResult
from brain_api.services import tenant_patient_cleanup as service


async def seed(db):
    target, other = Tenant(clinic_name="Target"), Tenant(clinic_name="Other")
    shared = MessagePatientAccount(email="shared@example.test")
    orphan = MessagePatientAccount(email="orphan@example.test")
    db.add_all([target, other, shared, orphan])
    await db.flush()
    target_patient = MessagePatient(tenant_id=target.id, account_id=shared.id, email=shared.email)
    outsider = MessagePatient(tenant_id=other.id, account_id=shared.id, email=shared.email)
    only_target = MessagePatient(tenant_id=target.id, account_id=orphan.id, email=orphan.email)
    pending = MessagePatient(tenant_id=target.id)
    db.add_all([target_patient, outsider, only_target, pending])
    await db.flush()
    login = MessagePatientSession(
        account_id=shared.id,
        tenant_id=target.id,
        patient_id=target_patient.id,
        token_hash="shared-hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db.add_all(
        [
            login,
            MessagePatientSession(
                account_id=orphan.id,
                patient_id=only_target.id,
                tenant_id=target.id,
                token_hash="orphan-hash",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
            MessagePatientSession(
                tenant_id=target.id,
                patient_id=pending.id,
                token_hash="legacy-hash",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
            MessagePendingSession(
                tenant_id=target.id,
                patient_id=pending.id,
                token_hash="pending-hash",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
            MessagePatientAccountOtp(
                email=orphan.email,
                code_hash="otp-hash",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            ),
            MessagePatientAccountOtp(
                email=shared.email,
                code_hash="other-otp-hash",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            ),
        ]
    )
    await db.commit()
    return target.id, other.id, outsider.id, shared.id, orphan.id, login.id


@pytest.mark.asyncio
async def test_preview_and_cleanup_keep_shared_accounts_and_other_clinic(db_session, monkeypatch):
    target, other, outsider, shared, orphan, login = await seed(db_session)
    calls = []

    async def product(name, tid, payload=None):
        calls.append((name, tid, payload is not None))
        return ProductResult(status="completed" if payload else "ready", counts={"patients": 1})

    monkeypatch.setattr(service, "_call_product", product)
    preview = await service.cleanup_patients(db_session, target)
    assert preview.status == "ready"
    assert preview.products["brain"].counts["message_patients"] == 3
    assert len(list(await db_session.scalars(select(MessagePatient)))) == 4
    result = await service.cleanup_patients(
        db_session, target, payload=CleanupRequest(confirm=True, clinic_name="Target")
    )
    assert result.status == "completed"
    db_session.expire_all()
    assert await db_session.get(MessagePatient, outsider) is not None
    assert await db_session.get(MessagePatientAccount, shared) is not None
    assert await db_session.get(MessagePatientAccount, orphan) is None
    assert await db_session.get(Tenant, target) is not None
    assert await db_session.get(Tenant, other) is not None
    kept_login = await db_session.get(MessagePatientSession, login)
    assert kept_login is not None and kept_login.patient_id is kept_login.tenant_id is None
    assert len(list(await db_session.scalars(select(MessagePatientSession)))) == 1
    assert len(list(await db_session.scalars(select(MessagePatientAccountOtp)))) == 1
    assert list(await db_session.scalars(select(MessagePendingSession))) == []
    assert "@example.test" not in result.model_dump_json()
    repeated = await service.cleanup_patients(
        db_session, target, payload=CleanupRequest(confirm=True, clinic_name="Target")
    )
    assert repeated.status == "completed"
    assert all(value == 0 for value in repeated.products["brain"].counts.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["preview", "execute"])
async def test_failed_product_keeps_brain_and_allows_retry(db_session, monkeypatch, failure_phase):
    target, *_ = await seed(db_session)
    calls = []

    async def failing(name, tid, payload=None):
        calls.append((name, bool(payload)))
        if name == "precheck" and (bool(payload) == (failure_phase == "execute")):
            return ProductResult(status="failed", blockers=["test_failure"])
        return ProductResult(status="completed" if payload else "ready")

    monkeypatch.setattr(service, "_call_product", failing)
    result = await service.cleanup_patients(
        db_session, target, payload=CleanupRequest(confirm=True, clinic_name="Target")
    )
    assert result.status == ("blocked" if failure_phase == "preview" else "partial")
    assert (
        len(
            list(
                await db_session.scalars(
                    select(MessagePatient).where(MessagePatient.tenant_id == target)
                )
            )
        )
        == 3
    )
    if failure_phase == "preview":
        assert not any(executed for _, executed in calls)


@pytest.mark.asyncio
async def test_confirmation_guards_run_before_any_product_call(db_session, monkeypatch):
    from fastapi import HTTPException

    target, *_ = await seed(db_session)

    async def unexpected(*args, **kwargs):
        pytest.fail("Product must not be contacted before confirmation")

    monkeypatch.setattr(service, "_call_product", unexpected)
    for payload, code in [
        (CleanupRequest(clinic_name="Target"), 400),
        (CleanupRequest(confirm=True, clinic_name="Other"), 409),
    ]:
        with pytest.raises(HTTPException) as error:
            await service.cleanup_patients(db_session, target, payload=payload)
        assert error.value.status_code == code


@pytest.mark.asyncio
async def test_doctor_cannot_cleanup(client):
    from tests.test_rbac import OWNER_A_EMAIL, OWNER_A_PASSWORD, _bearer, _token

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    path = f"/admin/tenants/{uuid4()}/patient-cleanup"
    assert (await client.get(path + "/preview", headers=_bearer(token))).status_code == 403
    assert (
        await client.post(
            path, headers=_bearer(token), json={"confirm": True, "clinic_name": "Target"}
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_product_transport_uses_existing_service_auth_and_validates_results(monkeypatch):
    import httpx

    from brain_api.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "SECRETARIA_BASE_URL", "https://secretaria.test")
    monkeypatch.setattr(settings, "SECRETARIA_API_KEY", "test-service-key")
    original = httpx.AsyncClient
    tid = uuid4()

    def handler(request):
        assert request.headers["X-Internal-Api-Key"] == "test-service-key"
        assert request.url.path == f"/internal/tenants/{tid}/patient-cleanup/preview"
        return httpx.Response(200, json={"status": "completed", "counts": {"patients": 1}})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    # completed is not a valid PREVIEW reply; never trust it as permission to delete.
    result = await service._call_product("secretaria", tid)
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_cleanup_routes_require_login(client):
    path = f"/admin/tenants/{uuid4()}/patient-cleanup"
    assert (await client.get(path + "/preview")).status_code == 401
    assert (
        await client.post(path, json={"confirm": True, "clinic_name": "Clinic"})
    ).status_code == 401
