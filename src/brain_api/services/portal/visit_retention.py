"""Retention of EMPTY Portal visits (TASK-042, owner 2026-10-09).

"cada conversa dessa que é apenas iniciada não seja gravada no banco de forma permanente."

Opening a clinic's link mints a visit (`open_pending_session`): a `MessagePendingSession` and
a `MessagePatient` with no address. A visitor who never typed anything leaves both behind
forever, and secretarIA keeps a patient row, a conversation and the bot's greeting for it.
This job removes those visits once they are older than `VISIT_RETENTION_HOURS` (24 h).

WHAT IS "EMPTY", AND WHO DECIDES
brain-api cannot see the conversation, so emptiness is decided in two halves:
  * here, by what brain-api holds: no claimed e-mail (`email`/`claimed_at` NULL), no code
    requested, not verified, not superseded, identity with no address, no account, no name,
    no login row and no consent event — and the visit expired more than `_EXPIRY_GRACE` ago
    (its cookie is dead, and a message sent in its last second has left secretarIA's queue);
  * by secretarIA (`POST /internal/brain-message/visits/discard`), which refuses with 409 if
    the patient wrote a single message, booked anything or holds a slot.

PRECHECK: brain-api KNOWS WHETHER THE PATIENT TYPED (owner, 2026-10-09)
The same visit can talk to PreCheck (its tab, its QR code), and PreCheck has no "is this visit
empty?" route — secretarIA would answer "only my greeting" for a visitor who answered the whole
questionnaire in PreCheck (review C1). But every patient message to either product passes
through brain-api's relay, which stamps `MessagePatient.patient_wrote_at`; PreCheck's own
automatic greeting never does. So: a stamped identity is never a candidate, at any clinic.
Identities created before the stamp existed (`activity_tracked = False`) cannot prove they
never wrote to PreCheck, so at a clinic with PreCheck they are left alone; at a clinic without
PreCheck secretarIA's answer is enough, as before. PreCheck's own empty session (its greeting)
is left in PreCheck: it holds nothing the patient said.

A VISIT WITH A TYPED E-MAIL IS NEVER DELETED (owner, 2026-10-09: "nunca apague esse email
digitado e nunca verificado"). That is the `email IS NULL` / `claimed_at IS NULL` filter, and
it covers `message_patient_account_otps` too: nothing here touches that table.

ORDER, SO NOTHING IS LOST
1. stamp `retention_discard_requested_at` and commit (the intent);
2. ask secretarIA;
3. `discarded` -> delete brain-api's visit + identity (guards re-checked inside the DELETE);
   409 -> stamp `retention_kept_at`, kept for good;
   `absent` on a visit stamped in an EARLIER round -> secretarIA already did its half (lost
   response, failed local delete): delete brain-api's side; `absent` on a first ask ->
   secretarIA never had the visit: kept for good;
   failure -> untouched, retried next round (attempted visits go to the back of the queue).
Dry-run counts every candidate and calls nobody — calling secretarIA would already delete.

The log is counts only: never a handle, never an address.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, exists, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.models.patient_access import (
    MessagePatient,
    MessagePatientSession,
    MessagePendingSession,
    PatientConsentEvent,
)
from brain_api.services import message_switchboard
from brain_api.services.entitlements import resolve_entitlement

logger = get_logger(__name__)

# Any constant works; it only has to be the same in every brain-api process.
_ADVISORY_LOCK_KEY = 42_042
# How long after expiry a visit is still left alone: covers a message sent in the visit's
# last second that is still in secretarIA's queue when the job runs (review I1).
_EXPIRY_GRACE = timedelta(minutes=15)
# A round stops after this many failures in a row (secretarIA down, or without the route
# during the rollout) instead of making the same failing call for the whole batch.
_MAX_CONSECUTIVE_FAILURES = 3


@dataclass(frozen=True)
class EmptyVisit:
    pending_id: UUID
    patient_id: UUID
    tenant_id: UUID
    asked_before: bool


@dataclass
class RetentionRound:
    dry_run: bool
    candidates: int = 0
    discarded: int = 0
    kept_not_empty: int = 0
    kept_absent: int = 0
    failed: int = 0
    precheck_clinics: int = 0
    skipped_by_lock: bool = False


def _empty_visit_filter(now: datetime, older_than: timedelta) -> list:
    sibling = aliased(MessagePendingSession)
    return [
        MessagePendingSession.created_at < now - older_than,
        MessagePendingSession.expires_at < now - _EXPIRY_GRACE,
        # A typed e-mail is never deleted (owner). Both columns, belt and braces.
        MessagePendingSession.email.is_(None),
        MessagePendingSession.claimed_at.is_(None),
        MessagePendingSession.otp_requested_at.is_(None),
        MessagePendingSession.verified_at.is_(None),
        MessagePendingSession.superseded_by.is_(None),
        MessagePendingSession.retention_kept_at.is_(None),
        MessagePatient.tenant_id == MessagePendingSession.tenant_id,
        MessagePatient.email.is_(None),
        MessagePatient.account_id.is_(None),
        MessagePatient.name.is_(None),
        # The patient sent something to some product through the relay: never empty.
        MessagePatient.patient_wrote_at.is_(None),
        # One visit per identity by construction; anything else is not this job's shape.
        ~exists().where(
            sibling.patient_id == MessagePendingSession.patient_id,
            sibling.id != MessagePendingSession.id,
        ),
        ~exists().where(sibling.superseded_by == MessagePendingSession.patient_id),
        ~exists().where(MessagePatientSession.patient_id == MessagePendingSession.patient_id),
    ]


async def _precheck_clinics(session: AsyncSession, tenant_ids: set[UUID]) -> set[UUID]:
    """The clinics among `tenant_ids` that have PreCheck (untracked visits stay there)."""
    skipped = set()
    for tenant_id in tenant_ids:
        if (await resolve_entitlement(session, tenant_id)).products.precheck:
            skipped.add(tenant_id)
    return skipped


async def find_empty_visits(
    session: AsyncSession, *, now: datetime, older_than: timedelta, limit: int | None
) -> tuple[list[EmptyVisit], int]:
    """`(visits, PreCheck clinics among them)`: the oldest empty-looking visits. Reads only."""
    where = _empty_visit_filter(now, older_than)
    joined = select(MessagePendingSession.tenant_id).join(
        MessagePatient, MessagePatient.id == MessagePendingSession.patient_id
    )
    tenants = set(await session.scalars(joined.where(*where).distinct()))
    precheck = await _precheck_clinics(session, tenants)
    if precheck:
        where.append(
            or_(
                MessagePendingSession.tenant_id.notin_(precheck),
                MessagePatient.activity_tracked.is_(True),
            )
        )
    query = (
        select(
            MessagePendingSession.id,
            MessagePendingSession.patient_id,
            MessagePendingSession.tenant_id,
            MessagePendingSession.retention_discard_requested_at.is_not(None),
        )
        .join(MessagePatient, MessagePatient.id == MessagePendingSession.patient_id)
        .where(*where)
        # Never-attempted first, so visits that keep failing cannot stall the batch.
        .order_by(
            MessagePendingSession.retention_discard_requested_at.is_not(None),
            MessagePendingSession.created_at,
        )
    )
    if limit is not None:
        query = query.limit(limit)
    visits = [EmptyVisit(*row) for row in await session.execute(query)]
    if visits:
        # `subject_ref` is the handle as a string; compared in Python so the UUID's storage
        # format (dashes or not) never decides whether a consent trail protects a row.
        consented = set(
            await session.scalars(
                select(PatientConsentEvent.subject_ref).where(
                    PatientConsentEvent.subject_ref.in_([str(v.patient_id) for v in visits])
                )
            )
        )
        visits = [v for v in visits if str(v.patient_id) not in consented]
    return visits, len(precheck)


async def _stamp(session: AsyncSession, visit: EmptyVisit, **values) -> None:
    await session.execute(
        update(MessagePendingSession)
        .where(MessagePendingSession.id == visit.pending_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _delete_brain_side(session: AsyncSession, visit: EmptyVisit) -> bool:
    """Delete the visit and its identity, re-checking every guard in the DELETE itself."""
    gone = await session.execute(
        delete(MessagePendingSession)
        .where(
            MessagePendingSession.id == visit.pending_id,
            MessagePendingSession.email.is_(None),
            MessagePendingSession.claimed_at.is_(None),
            MessagePendingSession.verified_at.is_(None),
            ~exists().where(
                MessagePatient.id == MessagePendingSession.patient_id,
                MessagePatient.patient_wrote_at.is_not(None),
            ),
        )
        .execution_options(synchronize_session=False)
    )
    if gone.rowcount != 1:
        await session.rollback()
        return False
    await session.execute(
        delete(MessagePatient)
        .where(
            MessagePatient.id == visit.patient_id,
            MessagePatient.email.is_(None),
            MessagePatient.account_id.is_(None),
            ~exists().where(MessagePatientSession.patient_id == MessagePatient.id),
            ~exists().where(MessagePendingSession.patient_id == MessagePatient.id),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return True


async def run_retention_round(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    dry_run: bool | None = None,
) -> RetentionRound:
    """One bounded pass. Never raises for a single visit; the summary is logged once."""
    settings = get_settings()
    current = now or datetime.now(UTC)
    result = RetentionRound(
        dry_run=settings.VISIT_RETENTION_DRY_RUN if dry_run is None else dry_run
    )
    async with session_factory() as session:
        visits, result.precheck_clinics = await find_empty_visits(
            session,
            now=current,
            older_than=timedelta(hours=settings.VISIT_RETENTION_HOURS),
            # Dry-run reports the whole backlog: that number is what the owner signs off on.
            limit=None if result.dry_run else settings.VISIT_RETENTION_BATCH_SIZE,
        )
        result.candidates = len(visits)
        failures_in_a_row = 0
        for visit in [] if result.dry_run else visits:
            await _stamp(session, visit, retention_discard_requested_at=current)
            answer = await message_switchboard.discard_empty_visit(
                tenant_id=visit.tenant_id, visit_ref=str(visit.patient_id)
            )
            done_upstream = answer == message_switchboard.DISCARD_DISCARDED or (
                answer == message_switchboard.DISCARD_ABSENT and visit.asked_before
            )
            if done_upstream and await _delete_brain_side(session, visit):
                result.discarded += 1
                failures_in_a_row = 0
            elif answer == message_switchboard.DISCARD_NOT_EMPTY:
                await _stamp(session, visit, retention_kept_at=current)
                result.kept_not_empty += 1
                failures_in_a_row = 0
            elif answer == message_switchboard.DISCARD_ABSENT and not visit.asked_before:
                await _stamp(session, visit, retention_kept_at=current)
                result.kept_absent += 1
                failures_in_a_row = 0
            else:
                result.failed += 1
                failures_in_a_row += 1
                if (
                    answer == message_switchboard.DISCARD_UNCONFIGURED
                    or failures_in_a_row >= _MAX_CONSECUTIVE_FAILURES
                ):
                    break
    logger.info(
        "visit_retention_round",
        dry_run=result.dry_run,
        candidates=result.candidates,
        discarded=result.discarded,
        kept_not_empty=result.kept_not_empty,
        kept_absent=result.kept_absent,
        failed=result.failed,
        precheck_clinics=result.precheck_clinics,
    )
    return result


async def run_locked_round(
    session_factory: async_sessionmaker[AsyncSession], **kwargs
) -> RetentionRound:
    """`run_retention_round` behind a Postgres advisory lock, so two processes never overlap.

    The lock lives on its own AUTOCOMMIT connection: a session-level advisory lock needs no
    transaction, and an open one would sit "idle in transaction" for the whole round —
    exactly what a server-side idle timeout kills, silently releasing the lock (review I3).
    """
    engine = session_factory.kw.get("bind")
    if engine is None or engine.dialect.name != "postgresql":
        return await run_retention_round(session_factory, **kwargs)
    async with engine.connect() as raw:
        lock_conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
        got = await lock_conn.scalar(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
        )
        if not got:
            return RetentionRound(dry_run=True, skipped_by_lock=True)
        try:
            return await run_retention_round(session_factory, **kwargs)
        finally:
            await lock_conn.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY}
            )


async def retention_loop(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Run a round every `VISIT_RETENTION_INTERVAL_MINUTES` until cancelled."""
    settings = get_settings()
    logger.info(
        "visit_retention_loop_started",
        dry_run=settings.VISIT_RETENTION_DRY_RUN,
        interval_minutes=settings.VISIT_RETENTION_INTERVAL_MINUTES,
    )
    while True:
        try:
            await run_locked_round(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad round must not end the loop
            logger.error("visit_retention_round_failed", error=type(exc).__name__)
        await asyncio.sleep(settings.VISIT_RETENTION_INTERVAL_MINUTES * 60)
