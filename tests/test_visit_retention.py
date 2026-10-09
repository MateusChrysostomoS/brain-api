"""TASK-042: empty Portal visits are deleted after 24 h; a typed e-mail never is.

The job (`services/portal/visit_retention.py`) asks secretarIA first and deletes its own rows
only on `discarded`. These tests drive it with a fake secretarIA answer and a fixed clock.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from brain_api.core.database import Base
from brain_api.models import Tenant
from brain_api.models.entitlement import Entitlement
from brain_api.models.patient_access import (
    MessagePatient,
    MessagePatientAccount,
    MessagePatientAccountOtp,
    MessagePatientSession,
    MessagePendingSession,
    PatientConsentEvent,
)
from brain_api.services import message_switchboard
from brain_api.services.portal import visit_retention

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
TYPED = "digitado@exemplo.com"


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
def secretaria(monkeypatch):
    """A fake secretarIA: answers `answer` and records which handles it was asked about."""
    fake = type("Fake", (), {"answer": message_switchboard.DISCARD_DISCARDED, "asked": []})()

    async def _discard(*, tenant_id, visit_ref):
        fake.asked.append(visit_ref)
        return fake.answer

    monkeypatch.setattr(message_switchboard, "discard_empty_visit", _discard)
    return fake


async def _visit(
    maker, *, age_hours: float, email: str | None = None, name: str | None = None, **pending
) -> UUID:
    created = NOW - timedelta(hours=age_hours)
    pending.setdefault("claimed_at", created if email else None)
    pending.setdefault("expires_at", created + timedelta(hours=24))
    async with maker() as session:
        tenant = Tenant(clinic_name="Clinic")
        session.add(tenant)
        await session.flush()
        patient = MessagePatient(id=uuid4(), tenant_id=tenant.id, name=name, created_at=created)
        session.add(patient)
        await session.flush()
        session.add(
            MessagePendingSession(
                patient_id=patient.id,
                tenant_id=tenant.id,
                token_hash=uuid4().hex,
                email=email,
                created_at=created,
                **pending,
            )
        )
        await session.commit()
        return patient.id


async def _count(maker, model) -> int:
    async with maker() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def _run(maker):
    return await visit_retention.run_retention_round(maker, now=NOW, dry_run=False)


async def test_a_25h_empty_visit_is_deleted_after_secretaria_confirms(maker, secretaria) -> None:
    handle = await _visit(maker, age_hours=25)

    result = await _run(maker)

    assert secretaria.asked == [str(handle)]
    assert result.discarded == 1
    assert await _count(maker, MessagePendingSession) == 0
    assert await _count(maker, MessagePatient) == 0


async def test_a_23h_visit_is_not_even_asked_about(maker, secretaria) -> None:
    await _visit(maker, age_hours=23)

    result = await _run(maker)

    assert secretaria.asked == []
    assert result.candidates == 0
    assert await _count(maker, MessagePatient) == 1


async def test_a_typed_unverified_email_is_never_deleted_even_after_a_year(
    maker, secretaria
) -> None:
    await _visit(maker, age_hours=24 * 365, email=TYPED)
    async with maker() as session:
        session.add(
            MessagePatientAccountOtp(
                email=TYPED, code_hash="0" * 64, expires_at=NOW - timedelta(days=300)
            )
        )
        await session.commit()

    result = await _run(maker)

    assert secretaria.asked == []
    assert result.candidates == 0
    async with maker() as session:
        assert (await session.scalar(select(MessagePendingSession.email))) == TYPED
    assert await _count(maker, MessagePatientAccountOtp) == 1


async def test_a_claimed_flag_alone_also_protects_the_visit(maker, secretaria) -> None:
    await _visit(maker, age_hours=48, claimed_at=NOW - timedelta(hours=47))
    assert (await _run(maker)).candidates == 0


async def test_verified_or_promoted_visits_and_accounts_never_go(maker, secretaria) -> None:
    await _visit(maker, age_hours=48, verified_at=NOW - timedelta(hours=47))
    handle = await _visit(maker, age_hours=48)
    async with maker() as session:
        account = MessagePatientAccount(email="conta@exemplo.com")
        session.add(account)
        await session.flush()
        (await session.get(MessagePatient, handle)).account_id = account.id
        await session.commit()

    result = await _run(maker)

    assert result.candidates == 0
    assert await _count(maker, MessagePatient) == 2
    assert await _count(maker, MessagePatientAccount) == 1


async def test_an_identity_with_a_login_row_or_consent_is_kept(maker, secretaria) -> None:
    with_login = await _visit(maker, age_hours=48)
    with_consent = await _visit(maker, age_hours=48)
    async with maker() as session:
        patient = await session.get(MessagePatient, with_login)
        session.add(
            MessagePatientSession(
                patient_id=patient.id,
                tenant_id=patient.tenant_id,
                token_hash=uuid4().hex,
                expires_at=NOW + timedelta(days=1),
            )
        )
        other = await session.get(MessagePatient, with_consent)
        session.add(
            PatientConsentEvent(
                tenant_id=other.tenant_id, subject_ref=str(with_consent), kind="x", legal_basis="y"
            )
        )
        await session.commit()

    assert (await _run(maker)).candidates == 0


async def test_secretaria_saying_the_patient_wrote_keeps_the_visit_for_good(
    maker, secretaria
) -> None:
    await _visit(maker, age_hours=30)
    secretaria.answer = message_switchboard.DISCARD_NOT_EMPTY

    first = await _run(maker)
    second = await _run(maker)

    assert first.kept_not_empty == 1
    assert second.candidates == 0, "a kept visit is never asked about again"
    assert await _count(maker, MessagePatient) == 1


async def test_a_secretaria_failure_deletes_nothing_and_retries_next_round(
    maker, secretaria
) -> None:
    await _visit(maker, age_hours=30)
    secretaria.answer = message_switchboard.DISCARD_FAILED

    first = await _run(maker)
    assert first.failed == 1
    assert await _count(maker, MessagePendingSession) == 1

    secretaria.answer = message_switchboard.DISCARD_DISCARDED
    second = await _run(maker)
    assert second.discarded == 1
    assert await _count(maker, MessagePatient) == 0


async def test_running_twice_is_idempotent(maker, secretaria) -> None:
    await _visit(maker, age_hours=30)
    await _run(maker)
    again = await _run(maker)
    assert again.candidates == 0
    assert again.discarded == 0


async def test_dry_run_counts_and_calls_nobody(maker, secretaria) -> None:
    await _visit(maker, age_hours=30)
    result = await visit_retention.run_retention_round(maker, now=NOW, dry_run=True)
    assert result.candidates == 1
    assert secretaria.asked == []
    assert await _count(maker, MessagePatient) == 1


async def test_the_batch_is_bounded(maker, secretaria, monkeypatch) -> None:
    from brain_api.config import get_settings

    for _ in range(3):
        await _visit(maker, age_hours=30)
    monkeypatch.setattr(get_settings(), "VISIT_RETENTION_BATCH_SIZE", 2)
    assert (await _run(maker)).discarded == 2
    assert await _count(maker, MessagePatient) == 1


# --- Review findings (REVIEW.md C1, I1, I2, I4) -------------------------------------------


async def test_absent_on_a_first_ask_keeps_the_visit_for_good(maker, secretaria) -> None:
    await _visit(maker, age_hours=30)
    secretaria.answer = message_switchboard.DISCARD_ABSENT

    first = await _run(maker)
    second = await _run(maker)

    assert first.kept_absent == 1
    assert second.candidates == 0
    assert await _count(maker, MessagePatient) == 1


async def test_absent_after_an_earlier_ask_finishes_the_delete(maker, secretaria) -> None:
    """secretarIA deleted but its answer was lost: the next `absent` is permission (I2)."""
    await _visit(maker, age_hours=30)
    secretaria.answer = message_switchboard.DISCARD_FAILED
    await _run(maker)

    secretaria.answer = message_switchboard.DISCARD_ABSENT
    result = await _run(maker)

    assert result.discarded == 1
    assert await _count(maker, MessagePatient) == 0


async def _with_precheck(maker, handle: UUID) -> None:
    async with maker() as session:
        tenant_id = (await session.get(MessagePatient, handle)).tenant_id
        session.add(Entitlement(tenant_id=tenant_id, precheck_enabled=True))
        await session.commit()


async def _set_patient(maker, handle: UUID, **values) -> None:
    async with maker() as session:
        patient = await session.get(MessagePatient, handle)
        for key, value in values.items():
            setattr(patient, key, value)
        await session.commit()


async def test_a_precheck_clinic_visit_where_nobody_typed_is_cleaned(maker, secretaria) -> None:
    """PreCheck's automatic greeting does not count: no patient message -> cleaned (owner)."""
    handle = await _visit(maker, age_hours=30)
    await _with_precheck(maker, handle)

    result = await _run(maker)

    assert secretaria.asked == [str(handle)]
    assert result.discarded == 1
    assert result.precheck_clinics == 1


async def test_a_visit_where_the_patient_typed_is_never_a_candidate(maker, secretaria) -> None:
    """Typed in PreCheck (or anywhere): kept, at any clinic, without even asking secretarIA."""
    with_precheck = await _visit(maker, age_hours=30)
    await _with_precheck(maker, with_precheck)
    without = await _visit(maker, age_hours=30)
    for handle in (with_precheck, without):
        await _set_patient(maker, handle, patient_wrote_at=NOW - timedelta(hours=29))

    result = await _run(maker)

    assert result.candidates == 0
    assert secretaria.asked == []
    assert await _count(maker, MessagePatient) == 2


async def test_an_untracked_visit_is_kept_only_where_precheck_exists(maker, secretaria) -> None:
    """Before the stamp existed nobody recorded PreCheck typing: those visits stay there."""
    at_precheck = await _visit(maker, age_hours=30)
    await _with_precheck(maker, at_precheck)
    elsewhere = await _visit(maker, age_hours=30)
    for handle in (at_precheck, elsewhere):
        await _set_patient(maker, handle, activity_tracked=False)

    await _run(maker)

    assert secretaria.asked == [str(elsewhere)]
    async with maker() as session:
        assert await session.get(MessagePatient, at_precheck) is not None
        assert await session.get(MessagePatient, elsewhere) is None


async def test_a_message_typed_during_the_round_blocks_the_local_delete(maker, monkeypatch):
    handle = await _visit(maker, age_hours=30)

    async def _discard_while_patient_types(*, tenant_id, visit_ref):
        await _set_patient(maker, handle, patient_wrote_at=NOW)
        return message_switchboard.DISCARD_DISCARDED

    monkeypatch.setattr(message_switchboard, "discard_empty_visit", _discard_while_patient_types)
    result = await _run(maker)

    assert result.discarded == 0
    assert await _count(maker, MessagePendingSession) == 1


async def test_mark_patient_wrote_stamps_once_and_never_an_account_identity(maker) -> None:
    from brain_api.services.portal import patient_access

    visit = await _visit(maker, age_hours=1)
    account_row = await _visit(maker, age_hours=1)
    await _set_patient(maker, account_row, email="conta@exemplo.com")
    async with maker() as session:
        await patient_access.mark_patient_wrote(session, visit)
        await patient_access.mark_patient_wrote(session, account_row)
        first = (await session.get(MessagePatient, visit)).patient_wrote_at
    async with maker() as session:
        await patient_access.mark_patient_wrote(session, visit)
    async with maker() as session:
        again = await session.get(MessagePatient, visit)
        assert first is not None and again.patient_wrote_at == first
        assert (await session.get(MessagePatient, account_row)).patient_wrote_at is None


async def test_a_visit_just_past_expiry_waits_for_the_grace(maker, secretaria) -> None:
    """A message sent in the visit's last second may still be in secretarIA's queue (I1)."""
    await _visit(maker, age_hours=30, expires_at=NOW - timedelta(minutes=5))
    assert (await _run(maker)).candidates == 0


async def test_code_requested_superseded_or_named_visits_are_not_candidates(
    maker, secretaria
) -> None:
    await _visit(maker, age_hours=30, otp_requested_at=NOW - timedelta(hours=29))
    await _visit(maker, age_hours=30, name="Maria")
    target = await _visit(maker, age_hours=500, email=TYPED)
    await _visit(maker, age_hours=30, superseded_by=target)
    assert (await _run(maker)).candidates == 0


async def test_an_email_claimed_while_secretaria_answers_blocks_the_local_delete(
    maker, monkeypatch
) -> None:
    await _visit(maker, age_hours=30)

    async def _discard_and_race(*, tenant_id, visit_ref):
        async with maker() as session:
            row = await session.scalar(select(MessagePendingSession))
            row.email, row.claimed_at = TYPED, NOW
            await session.commit()
        return message_switchboard.DISCARD_DISCARDED

    monkeypatch.setattr(message_switchboard, "discard_empty_visit", _discard_and_race)
    result = await _run(maker)

    assert result.discarded == 0
    assert result.failed == 1
    async with maker() as session:
        assert (await session.scalar(select(MessagePendingSession.email))) == TYPED
    assert await _count(maker, MessagePatient) == 1


async def test_dry_run_counts_the_whole_backlog_past_the_batch(
    maker, secretaria, monkeypatch
) -> None:
    from brain_api.config import get_settings

    for _ in range(3):
        await _visit(maker, age_hours=30)
    monkeypatch.setattr(get_settings(), "VISIT_RETENTION_BATCH_SIZE", 2)
    result = await visit_retention.run_retention_round(maker, now=NOW, dry_run=True)
    assert result.candidates == 3
    assert secretaria.asked == []


async def test_a_round_stops_after_three_failures_in_a_row(maker, secretaria) -> None:
    for _ in range(5):
        await _visit(maker, age_hours=30)
    secretaria.answer = message_switchboard.DISCARD_FAILED
    result = await _run(maker)
    assert result.failed == 3
    assert len(secretaria.asked) == 3


# --- The wire: how secretarIA's answers map (deploy-order safety net) ----------------------


def _wire(monkeypatch, transport: httpx.MockTransport) -> None:
    monkeypatch.setattr(
        message_switchboard,
        "_upstream",
        lambda product: ("http://secretaria", {"X-Internal-Api-Key": "k"}),
    )
    real = httpx.AsyncClient

    def _client(**kwargs):
        return real(transport=transport, **kwargs)

    monkeypatch.setattr(message_switchboard.httpx, "AsyncClient", _client)


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (200, {"status": "discarded"}, message_switchboard.DISCARD_DISCARDED),
        (200, {"status": "absent"}, message_switchboard.DISCARD_ABSENT),
        (200, {"status": "queued"}, message_switchboard.DISCARD_FAILED),
        (200, None, message_switchboard.DISCARD_FAILED),
        (409, {"detail": "visit_not_empty"}, message_switchboard.DISCARD_NOT_EMPTY),
        (404, {"detail": "Not Found"}, message_switchboard.DISCARD_FAILED),
        (422, {"detail": []}, message_switchboard.DISCARD_FAILED),
        (500, None, message_switchboard.DISCARD_FAILED),
    ],
)
async def test_discard_client_maps_every_answer(monkeypatch, status_code, body, expected):
    seen = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if body is None:
            return httpx.Response(status_code, content=b"<html>oops</html>")
        return httpx.Response(status_code, json=body)

    _wire(monkeypatch, httpx.MockTransport(_handler))
    tenant_id = uuid4()

    answer = await message_switchboard.discard_empty_visit(tenant_id=tenant_id, visit_ref="h-1")

    assert answer == expected
    assert seen[0].url.path == "/internal/brain-message/visits/discard"
    assert seen[0].headers["x-internal-api-key"] == "k"
    assert json.loads(seen[0].content) == {"tenant_id": str(tenant_id), "external_id": "h-1"}


async def test_discard_client_never_raises_on_transport_errors(monkeypatch) -> None:
    def _boom(request):
        raise httpx.ConnectTimeout("slow")

    _wire(monkeypatch, httpx.MockTransport(_boom))
    answer = await message_switchboard.discard_empty_visit(tenant_id=uuid4(), visit_ref="h")
    assert answer == message_switchboard.DISCARD_FAILED


async def test_discard_client_unconfigured(monkeypatch) -> None:
    from brain_api.config import get_settings

    monkeypatch.setattr(get_settings(), "SECRETARIA_BASE_URL", "")
    answer = await message_switchboard.discard_empty_visit(tenant_id=uuid4(), visit_ref="h")
    assert answer == message_switchboard.DISCARD_UNCONFIGURED
