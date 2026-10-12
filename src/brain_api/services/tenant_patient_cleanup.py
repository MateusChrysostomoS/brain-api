"""Mesh cleanup: preflight all stores, erase products, then revoke Brain identities.

There is no distributed transaction. A partial failure keeps Brain identities
so the administrator can retry using the same tenant UUID. Remote operations
are idempotent; no global reset/subject-by-email endpoint is called.
"""

import hashlib
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.models.patient_access import (
    MessagePatient,
    MessagePatientAccount,
    MessagePatientAccountOtp,
    MessagePatientOtp,
    MessagePatientSession,
    MessagePendingSession,
    PatientConsentEvent,
)
from brain_api.models.privacy_request import PrivacyRequest
from brain_api.models.tenant import Tenant
from brain_api.schemas.tenant_patient_cleanup import CleanupRequest, CleanupResult, ProductResult

logger = get_logger(__name__)


async def _call_product(
    product: str, tenant_id: UUID, payload: CleanupRequest | None = None
) -> ProductResult:
    settings = get_settings()
    if product == "secretaria":
        base, key = settings.SECRETARIA_BASE_URL, settings.SECRETARIA_API_KEY
        header, path = "X-Internal-Api-Key", f"/internal/tenants/{tenant_id}/patient-cleanup"
    else:
        base, key = settings.PRECHECK_BASE_URL, settings.PRECHECK_INTERNAL_TOKEN
        header, path = "X-Internal-Token", f"/api/v1/internal/tenants/{tenant_id}/patient-cleanup"
    if not base or not key:
        return ProductResult(status="blocked", blockers=["service_unconfigured"])
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0)) as client:
            if payload is None:
                response = await client.get(
                    base.rstrip("/") + path + "/preview", headers={header: key}
                )
            else:
                response = await client.post(
                    base.rstrip("/") + path, headers={header: key}, json=payload.model_dump()
                )
        response.raise_for_status()
        result = ProductResult.model_validate(response.json())
        expected = (
            {"ready", "blocked", "not_provisioned"}
            if payload is None
            else {"completed", "failed", "blocked", "not_provisioned"}
        )
        if result.status not in expected:
            raise ValueError("unexpected cleanup status")
        return result
    except (httpx.HTTPError, ValidationError, ValueError):
        # Never echo upstream body/URL/exception: those may contain PII or credentials.
        return ProductResult(status="failed", blockers=["service_unavailable_or_invalid_response"])


async def _brain_scope(session: AsyncSession, tenant_id: UUID, *, lock_accounts: bool = False):
    patient_ids = select(MessagePatient.id).where(MessagePatient.tenant_id == tenant_id)
    account_ids = select(MessagePatient.account_id).where(
        MessagePatient.tenant_id == tenant_id, MessagePatient.account_id.is_not(None)
    )
    if lock_accounts:
        await session.scalars(
            select(MessagePatientAccount.id)
            .where(MessagePatientAccount.id.in_(account_ids))
            .order_by(MessagePatientAccount.id)
            .with_for_update()
        )
    other_accounts = select(MessagePatient.account_id).where(
        MessagePatient.tenant_id != tenant_id, MessagePatient.account_id.is_not(None)
    )
    orphan_ids = select(MessagePatientAccount.id).where(
        MessagePatientAccount.id.in_(account_ids), MessagePatientAccount.id.not_in(other_accounts)
    )
    # Materialize BEFORE deleting patients; these are not logged or returned.
    orphans = list(await session.scalars(orphan_ids))
    orphan_emails = select(MessagePatientAccount.email).where(MessagePatientAccount.id.in_(orphans))
    other_emails = select(MessagePatient.email).where(
        MessagePatient.tenant_id != tenant_id, MessagePatient.email.is_not(None)
    )
    safe_emails = select(MessagePatientAccount.email).where(
        MessagePatientAccount.email.in_(orphan_emails),
        MessagePatientAccount.email.not_in(other_emails),
    )
    # Keep the login of accounts with other clinics, even if its pinned clinic is removed.
    session_scope = or_(
        MessagePatientSession.tenant_id == tenant_id,
        MessagePatientSession.patient_id.in_(patient_ids),
    )
    shared_scope = (
        session_scope
        & MessagePatientSession.account_id.is_not(None)
        & (MessagePatientSession.account_id.not_in(orphans))
    )
    scopes = {
        MessagePendingSession: MessagePendingSession.tenant_id == tenant_id,
        MessagePatientOtp: MessagePatientOtp.tenant_id == tenant_id,
        PatientConsentEvent: PatientConsentEvent.tenant_id == tenant_id,
        MessagePatientSession: or_(
            session_scope & ~shared_scope, MessagePatientSession.account_id.in_(orphans)
        ),
        MessagePatientAccountOtp: MessagePatientAccountOtp.email.in_(safe_emails),
        MessagePatient: MessagePatient.tenant_id == tenant_id,
        MessagePatientAccount: MessagePatientAccount.id.in_(orphans),
    }
    counts = {
        model.__tablename__: int(
            await session.scalar(select(func.count()).select_from(model).where(scope)) or 0
        )
        for model, scope in scopes.items()
    }
    counts["shared_logins_detached"] = int(
        await session.scalar(
            select(func.count()).select_from(MessagePatientSession).where(shared_scope)
        )
        or 0
    )
    return scopes, shared_scope, counts


async def cleanup_patients(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    payload: CleanupRequest | None = None,
    actor_id: str | None = None,
) -> CleanupResult:
    if payload is not None and not payload.confirm:
        raise HTTPException(400, "Set confirm: true to proceed.")
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    if payload is not None:
        stmt = stmt.with_for_update()
    tenant = await session.scalar(stmt)
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    clinic_name = tenant.clinic_name
    if payload is not None and payload.clinic_name != clinic_name:
        raise HTTPException(409, "clinic_name_mismatch")
    scopes, shared_scope, counts = await _brain_scope(session, tenant_id)
    products = {"brain": ProductResult(status="ready", counts=counts)}
    for product in ("secretaria", "precheck"):
        products[product] = await _call_product(product, tenant_id)
    ready = all(
        result.status in {"ready", "not_provisioned"} and not result.blockers
        for result in products.values()
    )
    if payload is None:
        return CleanupResult(
            tenant_id=tenant_id,
            clinic_name=clinic_name,
            status="ready" if ready else "blocked",
            products=products,
        )
    if ready:
        for product in ("secretaria", "precheck"):
            # Re-resolve even absent products: provisioning may have happened since preview.
            products[product] = await _call_product(product, tenant_id, payload)
            if products[product].status not in {"completed", "not_provisioned"}:
                break
        completed = all(
            products[p].status in {"completed", "not_provisioned"} and not products[p].blockers
            for p in ("secretaria", "precheck")
        )
        if completed:
            try:
                scopes, shared_scope, counts = await _brain_scope(
                    session, tenant_id, lock_accounts=True
                )
                await session.execute(
                    update(MessagePatientSession)
                    .where(shared_scope)
                    .values(patient_id=None, tenant_id=None)
                )
                for model, scope in scopes.items():
                    await session.execute(delete(model).where(scope))
                products["brain"] = ProductResult(status="completed", counts=counts)
            except SQLAlchemyError:
                await session.rollback()
                products["brain"] = ProductResult(
                    status="failed", blockers=["database_delete_failed"]
                )
    completed = products["brain"].status == "completed"
    result = CleanupResult(
        tenant_id=tenant_id,
        clinic_name=clinic_name,
        status="completed" if completed else "partial" if ready else "blocked",
        products=products,
    )
    session.add(
        PrivacyRequest(
            kind="tenant_cleanup",
            subject_type="patient",
            subject_hash=hashlib.sha256(str(tenant_id).encode()).hexdigest(),
            requested_by=UUID(actor_id) if actor_id else None,
            status="completed" if completed else "partial",
            result={p: r.model_dump() for p, r in products.items()},
        )
    )
    try:
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        products["brain"] = ProductResult(status="failed", blockers=["audit_or_commit_failed"])
        result = CleanupResult(
            tenant_id=tenant_id,
            clinic_name=clinic_name,
            status="partial" if ready else "blocked",
            products=products,
        )
    logger.warning("tenant_patient_cleanup", tenant_id=str(tenant_id), status=result.status)
    return result
