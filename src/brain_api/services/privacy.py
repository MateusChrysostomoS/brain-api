"""LGPD cross-database erasure/export orchestrator (CONTRACTS.md §14).

Three services — `brain` (this database), `secretaria`, `precheck` — hold SEPARATE
databases with NO cross-DB FK/cascade by design (docs/cross-db-erasure.md). brain-api is
the orchestrator: it erases/collects its OWN rows locally, fans out to the other two
over their internal service-to-service surfaces, and persists exactly ONE
`privacy_requests` audit row per invocation (models/privacy_request.py) — the trail that
survives the erasure it records.

Fan-out contract (fixed; the other two sides are built independently, see the module
docstrings of `secretaria_internal.py` / `precheck_client.py` for the analogous
client-style precedents this mirrors):

  * secretaria (patient-only; it has no notion of a brain "user" — CONTRACTS §12.1):
      `X-Internal-Api-Key` = `SECRETARIA_API_KEY`, base `SECRETARIA_BASE_URL`.
      GET    /internal/privacy/tenants/{tenant_id}/subjects/{wa_id}/export -> bundle
      DELETE /internal/privacy/tenants/{tenant_id}/subjects/{wa_id}       -> {"erased": {...}}
  * precheck (identifies by email AND/OR patient_phone):
      `X-Internal-Token` = `PRECHECK_INTERNAL_TOKEN`, base `PRECHECK_BASE_URL`.
      POST /api/v1/internal/privacy/export {"email", "patient_phone"} -> bundle
      POST /api/v1/internal/privacy/erase  {"email", "patient_phone"} -> {"erased": {...}}

Every upstream call returns EITHER its own JSON body on success OR one of three markers
that never raise an HTTP error (a failing/unconfigured/inapplicable leg must never block
the others):
  * `{"status": "failed"}`              — network error or upstream 4xx/5xx.
  * `{"status": "skipped_unconfigured"}` — base URL / key / token unset on our side.
  * `{"status": "not_applicable"}`       — secretaria only, when the subject has no
    `wa_id`/`tenant_id` (a `subject_type="user"` request: secretaria holds no user
    identity to look up, so there is nothing to call — NOT a configuration problem, and
    it does not flip the overall result to "partial").

Idempotent by construction: erasing/exporting an already-erased subject re-runs the same
queries and upstream calls, which naturally yield zero counts / already-empty state.
"""

import hashlib
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal
from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.models import (
    DemoRequest,
    PrecheckAccountLink,
    PrivacyRequest,
    RefreshToken,
    Tenant,
    User,
)
from brain_api.models.user import ROLE_ADMIN
from brain_api.schemas.privacy import PrivacyRequestIn

logger = get_logger(__name__)

_SECRETARIA_KEY_HEADER = "X-Internal-Api-Key"
_PRECHECK_TOKEN_HEADER = "X-Internal-Token"

# Outcome markers that mean "this leg did not contribute" — used both to short-circuit a
# leg's own logic and to decide whether the overall result is "partial".
_FAILED = "failed"
_SKIPPED_UNCONFIGURED = "skipped_unconfigured"
_NOT_APPLICABLE = "not_applicable"
_NON_SUCCESS_STATUSES = (_FAILED, _SKIPPED_UNCONFIGURED)


# --- Subject identity: normalize + hash (NEVER store the raw value) -------------------


def _normalize_subject_key(
    subject_type: str, *, email: str | None, wa_id: str | None, tenant_id: UUID | None
) -> str:
    """A stable string identifying the subject, built ONLY to be hashed (never stored).

    Distinct subject_type/tenant/identifier combinations must normalize to distinct
    keys (so the audit hash is a meaningful de-dupe signal) while the SAME subject
    always re-normalizes to the SAME key (so re-running an erasure/export is traceable
    as "the same subject" in the audit trail).
    """
    if subject_type == "user":
        assert email is not None  # schema-guaranteed
        return f"user:{email.strip().lower()}"
    assert wa_id is not None and tenant_id is not None  # schema-guaranteed
    key = f"patient:{tenant_id}:{wa_id.strip()}"
    if email:
        key += f":{email.strip().lower()}"
    return key


def subject_hash(key: str) -> str:
    """sha256 hex of the normalized subject key — the ONLY subject reference ever stored."""
    return hashlib.sha256(key.encode()).hexdigest()


# --- Local brain erasure -----------------------------------------------------------


async def _erase_brain_user(session: AsyncSession, email: str) -> dict[str, Any]:
    """Erase a brain portal user by email (lower-cased) and any demo-request leads of theirs.

    Deleting a `users` row is FK-cascaded to `refresh_tokens` + `precheck_account_links`
    at the database level (ON DELETE CASCADE), but the dependent rows are also deleted
    explicitly here first — correct and idempotent regardless of whether the connected
    database actually enforces FK actions (e.g. SQLite without `PRAGMA foreign_keys=ON`,
    as in this repo's test suite).

    REFUSES with `409 admin_cannot_be_erased` if the target is a platform `admin` — an
    admin account must never be wiped through this subject-erasure path.
    """
    lowered = email.strip().lower()
    user = await session.scalar(select(User).where(User.email == lowered))
    counts = {"users": 0, "demo_requests": 0}

    if user is not None:
        if user.role == ROLE_ADMIN:
            raise HTTPException(status.HTTP_409_CONFLICT, "admin_cannot_be_erased")
        await session.execute(delete(RefreshToken).where(RefreshToken.user_id == user.id))
        await session.execute(
            delete(PrecheckAccountLink).where(PrecheckAccountLink.brain_user_id == user.id)
        )
        await session.delete(user)
        counts["users"] = 1

    demo_result = await session.execute(delete(DemoRequest).where(DemoRequest.email == lowered))
    counts["demo_requests"] = demo_result.rowcount or 0

    await session.commit()
    return {"erased": counts}


async def _erase_brain_patient(session: AsyncSession, email: str | None) -> dict[str, Any]:
    """Brain holds no patient rows: only demo-request leads, and only if an email was
    ALSO supplied for this patient (a wa_id alone matches nothing brain-side)."""
    counts = {"demo_requests": 0}
    if email:
        demo_result = await session.execute(
            delete(DemoRequest).where(DemoRequest.email == email.strip().lower())
        )
        counts["demo_requests"] = demo_result.rowcount or 0
        await session.commit()
    return {"erased": counts}


# --- Local brain export -------------------------------------------------------------


def _demo_request_dict(row: DemoRequest) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "email": row.email,
        "clinic": row.clinic,
        "profile": row.profile,
        "product_interest": row.product_interest,
        "message": row.message,
        "source": row.source,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }


async def _export_brain_user(session: AsyncSession, email: str) -> dict[str, Any]:
    """Brain's own contribution to a user export: the user row (id/email/name/role/
    created_at — deliberately NO `password_hash`), the tenant name, and their demo-
    request leads."""
    lowered = email.strip().lower()
    user = await session.scalar(select(User).where(User.email == lowered))
    user_out: dict[str, Any] | None = None
    tenant_name: str | None = None
    if user is not None:
        user_out = {
            "id": str(user.id),
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "created_at": user.created_at.isoformat(),
        }
        if user.tenant_id is not None:
            tenant = await session.get(Tenant, user.tenant_id)
            tenant_name = tenant.clinic_name if tenant is not None else None

    demo_rows = (
        await session.scalars(select(DemoRequest).where(DemoRequest.email == lowered))
    ).all()
    return {
        "user": user_out,
        "tenant_name": tenant_name,
        "demo_requests": [_demo_request_dict(r) for r in demo_rows],
    }


async def _export_brain_patient(session: AsyncSession, email: str | None) -> dict[str, Any]:
    """Brain holds no patient rows: only demo-request leads matched by email, if given."""
    demo_rows: list[DemoRequest] = []
    if email:
        demo_rows = list(
            (
                await session.scalars(
                    select(DemoRequest).where(DemoRequest.email == email.strip().lower())
                )
            ).all()
        )
    return {"demo_requests": [_demo_request_dict(r) for r in demo_rows]}


# --- Upstream: secretaria (patient-only, X-Internal-Api-Key) ------------------------


async def _call_secretaria(
    action: Literal["export", "erase"], tenant_id: UUID | None, wa_id: str | None
) -> dict[str, Any]:
    """GET/DELETE secretaria's `/internal/privacy/tenants/{tenant_id}/subjects/{wa_id}/*`.

    secretaria only knows PATIENTS (keyed by wa_id within a tenant) — a `subject_type
    ="user"` request carries neither, which is `not_applicable` here, not a failure.
    """
    if tenant_id is None or wa_id is None:
        return {"status": _NOT_APPLICABLE}

    settings = get_settings()
    base, key = settings.SECRETARIA_BASE_URL, settings.SECRETARIA_API_KEY
    if not base or not key:
        logger.warning("privacy_secretaria_unconfigured", action=action)
        return {"status": _SKIPPED_UNCONFIGURED}

    path = f"/internal/privacy/tenants/{tenant_id}/subjects/{wa_id}"
    method = "GET" if action == "export" else "DELETE"
    if action == "export":
        path += "/export"

    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=settings.SECRETARIA_TIMEOUT_SECONDS
        ) as client:
            resp = await client.request(method, path, headers={_SECRETARIA_KEY_HEADER: key})
    except httpx.RequestError:
        logger.warning("privacy_secretaria_unreachable", action=action)
        return {"status": _FAILED}

    if resp.status_code >= 400:
        logger.warning(
            "privacy_secretaria_upstream_error", action=action, upstream_status=resp.status_code
        )
        return {"status": _FAILED}
    return resp.json()


# --- Upstream: precheck (email and/or patient_phone, X-Internal-Token) --------------


async def _call_precheck(
    action: Literal["export", "erase"], email: str | None, patient_phone: str | None
) -> dict[str, Any]:
    """POST precheck's `/api/v1/internal/privacy/export|erase` with `{email, patient_phone}`.

    Always attempted for both subject types — the schema guarantees at least one of
    `email`/`patient_phone` (== `wa_id`) is present.
    """
    settings = get_settings()
    base, token = settings.PRECHECK_BASE_URL, settings.PRECHECK_INTERNAL_TOKEN
    if not base or not token:
        logger.warning("privacy_precheck_unconfigured", action=action)
        return {"status": _SKIPPED_UNCONFIGURED}

    path = f"/api/v1/internal/privacy/{action}"
    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=settings.PRECHECK_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(
                path,
                headers={_PRECHECK_TOKEN_HEADER: token},
                json={"email": email, "patient_phone": patient_phone},
            )
    except httpx.RequestError:
        logger.warning("privacy_precheck_unreachable", action=action)
        return {"status": _FAILED}

    if resp.status_code >= 400:
        logger.warning(
            "privacy_precheck_upstream_error", action=action, upstream_status=resp.status_code
        )
        return {"status": _FAILED}
    return resp.json()


# --- Orchestration --------------------------------------------------------------------


def _leg_ok(leg: dict[str, Any]) -> bool:
    """A leg counts against "completed" only for an actual failure or missing config —
    an inapplicable leg (`not_applicable`) is expected, not a partial result."""
    return leg.get("status") not in _NON_SUCCESS_STATUSES


def _summarize_counts(bundle: dict[str, Any]) -> dict[str, Any]:
    """Reduce an export bundle to COUNTS ONLY for the audit row — never the data itself.

    A `status` marker (failed/skipped_unconfigured/not_applicable) passes through as-is;
    every other key becomes a count: list length, `1` for a present scalar/object, `0`
    for `None`.
    """
    summary: dict[str, Any] = {}
    for k, v in bundle.items():
        if k == "status":
            summary[k] = v
        elif isinstance(v, list):
            summary[k] = len(v)
        else:
            summary[k] = 0 if v is None else 1
    return summary


async def erase_subject(
    session: AsyncSession, principal: Principal, payload: PrivacyRequestIn
) -> dict[str, Any]:
    """Erase a subject across brain + secretaria + precheck; persist ONE audit row.

    Brain erasure runs first and is the only leg that can raise (409 admin refusal) —
    it aborts BEFORE any upstream call and before the audit row is written, so refusing
    to erase an admin never gets logged as a (partial) privacy action.
    """
    subject_key = _normalize_subject_key(
        payload.subject_type, email=payload.email, wa_id=payload.wa_id, tenant_id=payload.tenant_id
    )
    s_hash = subject_hash(subject_key)

    if payload.subject_type == "user":
        assert payload.email is not None
        brain_result = await _erase_brain_user(session, payload.email)
    else:
        brain_result = await _erase_brain_patient(session, payload.email)

    is_patient = payload.subject_type == "patient"
    secretaria_result = await _call_secretaria(
        "erase",
        payload.tenant_id if is_patient else None,
        payload.wa_id if is_patient else None,
    )
    precheck_result = await _call_precheck(
        "erase", payload.email, payload.wa_id if is_patient else None
    )

    overall = "completed" if _leg_ok(secretaria_result) and _leg_ok(precheck_result) else "partial"

    row = PrivacyRequest(
        kind="erasure",
        subject_type=payload.subject_type,
        subject_hash=s_hash,
        requested_by=UUID(principal.user_id),
        status=overall,
        result={
            "brain": brain_result,
            "secretaria": secretaria_result,
            "precheck": precheck_result,
        },
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    # WARNING (this is a destructive, LGPD-relevant action): actor + subject_hash only —
    # NEVER the raw email/wa_id.
    logger.warning(
        "privacy_erasure_executed",
        actor_user_id=principal.user_id,
        subject_type=payload.subject_type,
        subject_hash=s_hash,
    )

    return {
        "status": overall,
        "brain": brain_result,
        "secretaria": secretaria_result,
        "precheck": precheck_result,
        "request_id": row.id,
    }


async def export_subject(
    session: AsyncSession, principal: Principal, payload: PrivacyRequestIn
) -> dict[str, Any]:
    """Collect a subject's data across brain + secretaria + precheck; persist ONE audit
    row that stores COUNTS of collected records, NOT the data itself. The response
    (unlike the audit row) carries the full aggregated bundle under each service key."""
    subject_key = _normalize_subject_key(
        payload.subject_type, email=payload.email, wa_id=payload.wa_id, tenant_id=payload.tenant_id
    )
    s_hash = subject_hash(subject_key)

    if payload.subject_type == "user":
        assert payload.email is not None
        brain_bundle = await _export_brain_user(session, payload.email)
    else:
        brain_bundle = await _export_brain_patient(session, payload.email)

    is_patient = payload.subject_type == "patient"
    secretaria_bundle = await _call_secretaria(
        "export",
        payload.tenant_id if is_patient else None,
        payload.wa_id if is_patient else None,
    )
    precheck_bundle = await _call_precheck(
        "export", payload.email, payload.wa_id if is_patient else None
    )

    overall = "completed" if _leg_ok(secretaria_bundle) and _leg_ok(precheck_bundle) else "partial"

    row = PrivacyRequest(
        kind="export",
        subject_type=payload.subject_type,
        subject_hash=s_hash,
        requested_by=UUID(principal.user_id),
        status=overall,
        result={
            "brain": _summarize_counts(brain_bundle),
            "secretaria": _summarize_counts(secretaria_bundle),
            "precheck": _summarize_counts(precheck_bundle),
        },
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    logger.info(
        "privacy_export_executed",
        actor_user_id=principal.user_id,
        subject_type=payload.subject_type,
        subject_hash=s_hash,
    )

    return {
        "status": overall,
        "brain": brain_bundle,
        "secretaria": secretaria_bundle,
        "precheck": precheck_bundle,
        "request_id": row.id,
    }
