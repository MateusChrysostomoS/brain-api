"""Patient-access service — the account, its code, its clinics, its sessions.

The rules each part follows:

1. THE CODE IS A CREDENTIAL. It is generated with `secrets`, stored only as SHA-256, compared
   in constant time and never logged. Every guess is SPENT by one conditional UPDATE before
   the comparison, so parallel guesses cannot outrun `PATIENT_OTP_MAX_ATTEMPTS`; a right code
   is burned by a compare-and-swap, so two requests carrying it open the account once.

2. NOTHING HERE DISTINGUISHES AN UNKNOWN SUBJECT. Every helper answers `None`/`False` alike
   for an unknown address, a wrong code, an expired code, an unknown clinic and a clinic with
   the channel off. The ROUTER answers identically for all of them.

3. THE ACCOUNT IS THE ADDRESS; A CLINIC ENTERS IT ONLY BY A GESTURE AIMED AT THAT CLINIC
   (2026-09-15, replacing discovery by e-mail). Membership is `MessagePatient.account_id`. A
   clinic joins by an invite (`add_clinic`). A row from before the account joins only when
   its OWN login session is used again, or when its identity carries a confirmed link
   (`_adopt`, migration 0020's rule) — never because the address matches. A first-contact
   consent cannot be the test either: every pre-account identity carries one.

4. A CLINIC IDENTITY'S ID NEVER CHANGES. Adding a clinic reuses the row that clinic already
   had for the address: `MessagePatient.id` is secretarIA's `external_id` and PreCheck's
   `session_ref`, and the conversation history lives under it.

5. A CONVERSATION MAY START BEFORE THE ADDRESS (2026-09-16). A visitor with no e-mail gets a
   `MessagePendingSession` and an identity with `email = NULL`; the address is CLAIMED on the
   visit by secretarIA mid-chat and only moves onto the identity when a code proves it. Rules
   3 and 4 are untouched by this: nothing joins an account without a code, and the handle the
   visitor was minted is the one the account keeps. See the last section of this module.

6. NO ROLLBACK INSIDE A HELPER. A rollback expires every instance the CALLER holds, and
   reading an expired attribute is implicit IO that async SQLAlchemy refuses (a 500). Races on
   unique keys are settled by `INSERT ... ON CONFLICT` (`_upsert`) instead, and nothing
   commits while the row lock a rotation takes is held.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import exists, insert, literal, or_, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from brain_api.config import get_settings
from brain_api.core.invite_codes import parse_invite
from brain_api.core.logging import get_logger
from brain_api.core.security import generate_refresh_token, hash_refresh_token
from brain_api.models import Tenant
from brain_api.models.patient_access import (
    CONSENT_KIND_ACCOUNT_LINK,
    CONSENT_KIND_CHANNEL_ACCESS,
    CONSENT_LEGAL_BASIS_PENDING,
    MessagePatient,
    MessagePatientAccount,
    MessagePatientAccountOtp,
    MessagePatientSession,
    MessagePendingSession,
    PatientConsentEvent,
)

logger = get_logger(__name__)


def normalize_email(raw: str) -> str:
    """The one spelling an address is stored and looked up under (`users.email`'s rule)."""
    return raw.strip().lower()


def generate_otp_code() -> str:
    """A zero-padded decimal code of `PATIENT_OTP_LENGTH` digits, from `secrets`.

    Zero-padding matters: dropping a leading zero would shrink the space AND produce a code
    the patient cannot retype as shown. `randbelow` over the full range keeps every value
    equally likely.
    """
    digits = max(4, get_settings().PATIENT_OTP_LENGTH)
    return f"{secrets.randbelow(10**digits):0{digits}d}"


def _hash_code(code: str) -> str:
    """SHA-256 hex, the storage form. A 6-digit code IS guessable, so what protects it is
    `PATIENT_OTP_MAX_ATTEMPTS` plus the short expiry, never the hash's cost."""
    return hash_refresh_token(code)


def _as_utc(dt: datetime) -> datetime:
    """Normalize a DB datetime for comparison (SQLite returns naive; Postgres aware)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _upsert(session: AsyncSession, model: type):
    """An INSERT that can say ON CONFLICT — Postgres in production, SQLite in the suite."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql.insert(model)
    if dialect == "sqlite":
        return sqlite.insert(model)
    raise NotImplementedError(f"no ON CONFLICT insert for dialect {dialect!r}")


async def channel_open_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant | None:
    """The tenant, iff it exists AND has the Brain-Message channel switched on.

    `tenants.brain_message_enabled` is the same flag `GET /entitlements` projects as
    `channels.brain_message`, so the invite gate and the thread gate cannot drift apart.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or not tenant.brain_message_enabled:
        return None
    return tenant


# --- The code: it opens the ACCOUNT ------------------------------------------------------


async def issue_account_otp(session: AsyncSession, email: str) -> str:
    """Mint a code for the address, persisting only its hash; return the RAW code to e-mail.

    One statement, `INSERT ... ON CONFLICT (email) DO UPDATE`: a double submit of the first
    request for an address cannot collide on the unique key. Issuing OVERWRITES any live
    challenge and resets `attempts` — a new code is a new secret, so the guesses spent on the
    old one taught nothing about it. No account is created here; only a verified code does.
    """
    address = normalize_email(email)
    code = generate_otp_code()
    fresh = {
        "code_hash": _hash_code(code),
        "expires_at": datetime.now(UTC)
        + timedelta(minutes=get_settings().PATIENT_OTP_EXPIRE_MINUTES),
        "attempts": 0,
        "consumed_at": None,
    }
    await session.execute(
        _upsert(session, MessagePatientAccountOtp)
        .values(id=uuid4(), email=address, **fresh)
        .on_conflict_do_update(index_elements=["email"], set_=fresh)
    )
    await session.commit()
    # Neither the address (personal data) nor the code (a credential) belongs in a log line.
    logger.info("patient_otp_issued")
    return code


async def verify_account_otp(session: AsyncSession, email: str, code: str) -> bool:
    """Burn a correct code for the address; `False` for every failure, alike.

    The attempt is SPENT before the comparison, by one conditional UPDATE
    (`attempts = attempts + 1 WHERE attempts < ceiling AND code_hash = <the one read>`): N
    guesses racing each other get at most `PATIENT_OTP_MAX_ATTEMPTS` comparisons between
    them, where incrementing a value read in Python let them overwrite each other's count.
    Naming the hash that was read also refuses a code re-issued mid-request. A correct guess
    is then burned by a compare-and-swap on `consumed_at`, so it opens the account once.
    """
    address = normalize_email(email)
    now = datetime.now(UTC)
    ceiling = get_settings().PATIENT_OTP_MAX_ATTEMPTS
    challenge = await session.scalar(
        select(MessagePatientAccountOtp).where(MessagePatientAccountOtp.email == address)
    )
    if challenge is None or challenge.consumed_at is not None:
        return False
    if _as_utc(challenge.expires_at) <= now or challenge.attempts >= ceiling:
        return False
    challenge_id, expected = challenge.id, challenge.code_hash

    spent = await session.execute(
        update(MessagePatientAccountOtp)
        .where(
            MessagePatientAccountOtp.id == challenge_id,
            MessagePatientAccountOtp.code_hash == expected,
            MessagePatientAccountOtp.consumed_at.is_(None),
            MessagePatientAccountOtp.attempts < ceiling,
        )
        .values(attempts=MessagePatientAccountOtp.attempts + 1)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if spent.rowcount != 1:
        return False
    if not secrets.compare_digest(expected, _hash_code(code)):
        logger.info("patient_otp_rejected")
        return False

    burned = await session.execute(
        update(MessagePatientAccountOtp)
        .where(
            MessagePatientAccountOtp.id == challenge_id,
            MessagePatientAccountOtp.code_hash == expected,
            MessagePatientAccountOtp.consumed_at.is_(None),
        )
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return burned.rowcount == 1


# --- The account -------------------------------------------------------------------------


async def _ensure_account(session: AsyncSession, address: str) -> MessagePatientAccount:
    """The account row of an address, created if missing. No commit, and no rollback: a
    concurrent creation of the same account is absorbed by `ON CONFLICT DO NOTHING`."""
    await session.execute(
        _upsert(session, MessagePatientAccount)
        .values(id=uuid4(), email=address)
        .on_conflict_do_nothing(index_elements=["email"])
    )
    account = await session.scalar(
        select(MessagePatientAccount).where(MessagePatientAccount.email == address)
    )
    if account is None:  # pragma: no cover - the row was just inserted or already there
        raise RuntimeError("patient account vanished after its upsert")
    return account


async def open_account(session: AsyncSession, email: str) -> MessagePatientAccount:
    """Get-or-create the account of an address a code JUST proved, stamp it, commit.

    Adopts the address's pre-account identities that carry a confirmed link — nothing else
    (`_adopt`). The clinic the patient came in through is added by the caller (`add_clinic`).
    """
    account = await _ensure_account(session, normalize_email(email))
    account.last_seen_at = datetime.now(UTC)
    await _adopt(session, account, own=None)
    await session.commit()
    return account


async def _adopt(
    session: AsyncSession, account: MessagePatientAccount, *, own: MessagePatient | None
) -> None:
    """Bring pre-account identities of the address into the account. No commit.

    Exactly two kinds join (migration 0020 applies the same rule to the rows it finds):

    - `own` — the identity of a pre-account login session that is being USED right now (its
      cookie renewed). That login already opened this clinic, so it keeps opening it;
    - an identity carrying a `brain_message_account_link` event: the patient confirmed it by
      name, and every login of the address already reopened it.

    Anything else that merely shares the address stays out, including a clinic the address
    once logged in at separately: from this login it was an unanswered question (or a "no",
    which the old portal never recorded). It comes back through its own session, or through
    its link or code — with the same id and history. Then every session of the account's
    identities is stamped with the account, so tokens issued before it keep working.
    """
    candidates = (
        await session.scalars(
            select(MessagePatient).where(
                MessagePatient.email == account.email,
                MessagePatient.account_id.is_(None),
            )
        )
    ).all()
    adopted: list[MessagePatient] = []
    if candidates:
        # Matched in Python, not with a SQL cast: `subject_ref` holds the hyphenated UUID
        # string, which is not how every dialect renders a UUID column as text.
        linked = {
            (tenant_id, subject_ref)
            for tenant_id, subject_ref in (
                await session.execute(
                    select(PatientConsentEvent.tenant_id, PatientConsentEvent.subject_ref).where(
                        PatientConsentEvent.kind == CONSENT_KIND_ACCOUNT_LINK,
                        PatientConsentEvent.subject_ref.in_([str(p.id) for p in candidates]),
                    )
                )
            ).all()
        }
        adopted = [
            p
            for p in candidates
            if (own is not None and p.id == own.id) or (p.tenant_id, str(p.id)) in linked
        ]
        for patient in adopted:
            patient.account_id = account.id
        if adopted:
            await session.flush()
    await session.execute(
        update(MessagePatientSession)
        .where(
            MessagePatientSession.account_id.is_(None),
            MessagePatientSession.patient_id.in_(
                select(MessagePatient.id).where(MessagePatient.account_id == account.id)
            ),
        )
        .values(account_id=account.id)
        .execution_options(synchronize_session=False)
    )
    if adopted:
        # A count only: tenant ids of several clinics on one line would join them.
        logger.info("patient_account_identities_adopted", count=len(adopted))


# --- Invites: a clinic enters the account ------------------------------------------------


async def resolve_invite(session: AsyncSession, raw: str) -> Tenant | None:
    """The open clinic a pasted link, short code or UUID names, or `None` for every failure.

    Unparseable, unknown and channel-off answer the same `None`, so the invite routes cannot
    tell anyone which clinics exist or use Brain-Message.
    """
    parsed = parse_invite(raw)
    if parsed is None:
        return None
    if isinstance(parsed, UUID):
        return await channel_open_tenant(session, parsed)
    tenant = await session.scalar(select(Tenant).where(Tenant.patient_invite_code == parsed))
    if tenant is None or not tenant.brain_message_enabled:
        return None
    return tenant


async def add_clinic(
    session: AsyncSession, account: MessagePatientAccount, tenant: Tenant
) -> MessagePatient | None:
    """Put `tenant` in the account and return the account's identity there. Idempotent.

    The identity is the one that clinic ALREADY had for the address when there is one (its id
    is the sibling services' handle), otherwise a new row — created with `ON CONFLICT DO
    NOTHING`, so a double tap never fails on the unique `(tenant, e-mail)` key. Its
    `brain_message_channel_access` consent is recorded exactly once, ever, by an
    `INSERT ... SELECT ... WHERE NOT EXISTS` taken under the identity's row lock: atomic on
    SQLite, serialized by the lock on Postgres. Commits. `None` only for an identity bound to
    ANOTHER account, which the unique address makes unreachable; refused rather than moved.
    """
    now = datetime.now(UTC)
    account_id, address, tenant_id = account.id, account.email, tenant.id
    await session.execute(
        _upsert(session, MessagePatient)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            email=address,
            account_id=account_id,
            last_seen_at=now,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "email"])
    )
    patient = await session.scalar(
        select(MessagePatient)
        .where(MessagePatient.tenant_id == tenant_id, MessagePatient.email == address)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if patient is None or patient.account_id not in (None, account_id):
        return None
    patient.account_id = account_id
    patient.last_seen_at = now
    subject_ref = str(patient.id)

    columns = PatientConsentEvent.__table__.c
    recorded = await session.execute(
        insert(PatientConsentEvent).from_select(
            ["id", "tenant_id", "subject_ref", "kind", "legal_basis"],
            select(
                literal(uuid4(), columns.id.type),
                literal(tenant_id, columns.tenant_id.type),
                literal(subject_ref, columns.subject_ref.type),
                literal(CONSENT_KIND_CHANNEL_ACCESS, columns.kind.type),
                literal(CONSENT_LEGAL_BASIS_PENDING, columns.legal_basis.type),
            ).where(
                ~exists().where(
                    columns.tenant_id == tenant_id,
                    columns.subject_ref == subject_ref,
                    columns.kind == CONSENT_KIND_CHANNEL_ACCESS,
                )
            ),
        )
    )
    await session.commit()
    # The clinic and a count only — no handle: add events seconds apart from one client would
    # otherwise line up one person's handles across clinics.
    logger.info(
        "patient_clinic_added",
        tenant_id=str(tenant_id),
        consent_events_recorded=1 if recorded.rowcount == 1 else 0,
    )
    return patient


async def account_clinics(
    session: AsyncSession, account: MessagePatientAccount, tenant_id: UUID | None = None
) -> list[tuple[MessagePatient, Tenant]]:
    """The account's clinics whose channel is on (optionally just `tenant_id`), by name."""
    query = (
        select(MessagePatient, Tenant)
        .join(Tenant, Tenant.id == MessagePatient.tenant_id)
        .where(
            MessagePatient.account_id == account.id,
            Tenant.brain_message_enabled.is_(True),
        )
        .order_by(Tenant.clinic_name, Tenant.id)
    )
    if tenant_id is not None:
        query = query.where(MessagePatient.tenant_id == tenant_id)
    return [(patient, tenant) for patient, tenant in (await session.execute(query)).all()]


# --- The session (revocable leg) ---------------------------------------------------------


async def issue_patient_session(
    session: AsyncSession,
    account: MessagePatientAccount,
    *,
    opened_at: MessagePatient | None = None,
) -> tuple[str, UUID]:
    """Create the account's login row; return the RAW opaque token (once) and the row id.

    Byte-for-byte the `issue_refresh_token` scheme — high-entropy value out, SHA-256 in. Every
    token of this login carries the id as `sid`. `opened_at` pins the clinic the transition
    fields name (`login_clinic`).
    """
    raw = generate_refresh_token()
    row = MessagePatientSession(
        id=uuid4(),
        account_id=account.id,
        patient_id=opened_at.id if opened_at is not None else None,
        tenant_id=opened_at.tenant_id if opened_at is not None else None,
        token_hash=hash_refresh_token(raw),
        expires_at=datetime.now(UTC) + timedelta(days=get_settings().PATIENT_SESSION_EXPIRE_DAYS),
    )
    session.add(row)
    session_id = row.id
    await session.commit()
    return raw, session_id


async def pin_login_clinic(
    session: AsyncSession, login_session_id: UUID, patient: MessagePatient
) -> None:
    """Pin a login that names no clinic yet to `patient`. Never re-pins. Commits."""
    await session.execute(
        update(MessagePatientSession)
        .where(
            MessagePatientSession.id == login_session_id,
            MessagePatientSession.patient_id.is_(None),
        )
        .values(patient_id=patient.id, tenant_id=patient.tenant_id)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def login_clinic(
    session: AsyncSession, row: MessagePatientSession, account: MessagePatientAccount
) -> MessagePatient | None:
    """The clinic a login is pinned to — what the transition fields of the answers name.

    A login keeps the clinic it came in through. One opened by e-mail alone is pinned to the
    account's first clinic the first time it has one, and keeps it: the portal deployed on
    2026-09-14 drops an account whose top-level clinic changes between renewals, and "the
    first by name" would change whenever a clinic sorting earlier is added.
    """
    if row.patient_id is not None:
        patient = await session.get(MessagePatient, row.patient_id)
        if patient is not None and patient.account_id == account.id:
            return patient
        return None
    clinics = await account_clinics(session, account)
    if not clinics:
        return None
    patient = clinics[0][0]
    await pin_login_clinic(session, row.id, patient)
    return patient


def _is_live(row: MessagePatientSession, now: datetime) -> bool:
    return row.revoked_at is None and _as_utc(row.expires_at) > now


async def _row_account(
    session: AsyncSession, row: MessagePatientSession, *, adopt: bool
) -> MessagePatientAccount | None:
    """The account a session row belongs to. No commit.

    A row issued before the account model names only its identity. With `adopt`, the row
    is being USED (its cookie renewed): its identity joins its address's account, created if
    needed (`_adopt`). Without it, the address's account is only looked up.
    """
    if row.account_id is not None:
        return await session.get(MessagePatientAccount, row.account_id)
    if row.patient_id is None:
        return None
    patient = await session.get(MessagePatient, row.patient_id)
    if patient is None or patient.tenant_id != row.tenant_id:
        return None
    if patient.email is None:
        # A PENDING identity (no address proven yet, 0021). It has no `MessagePatientSession`
        # of its own, so this is unreachable today; the guard is here because the fallback
        # below would otherwise open an account keyed by `None`.
        return None
    if patient.account_id is not None:
        account = await session.get(MessagePatientAccount, patient.account_id)
    elif adopt:
        account = await _ensure_account(session, patient.email)
    else:
        account = await session.scalar(
            select(MessagePatientAccount).where(MessagePatientAccount.email == patient.email)
        )
    if account is not None and adopt:
        await _adopt(session, account, own=patient)
    return account


async def session_account(
    session: AsyncSession, row: MessagePatientSession
) -> MessagePatientAccount | None:
    """The account a live row belongs to, without adopting anything (read-only callers)."""
    return await _row_account(session, row, adopt=False)


async def _row_email(session: AsyncSession, row: MessagePatientSession) -> str | None:
    """The address a row belongs to — the key an account-wide revocation needs."""
    if row.account_id is not None:
        account = await session.get(MessagePatientAccount, row.account_id)
        return account.email if account is not None else None
    if row.patient_id is not None:
        patient = await session.get(MessagePatient, row.patient_id)
        return patient.email if patient is not None else None
    return None


async def find_patient_session(
    session: AsyncSession, raw_token: str
) -> MessagePatientSession | None:
    """The live row a raw cookie value names; `None` for unknown, expired and revoked alike."""
    row = await session.scalar(
        select(MessagePatientSession).where(
            MessagePatientSession.token_hash == hash_refresh_token(raw_token)
        )
    )
    if row is None or not _is_live(row, datetime.now(UTC)):
        return None
    return row


async def find_live_session(
    session: AsyncSession, session_id: UUID
) -> MessagePatientSession | None:
    """The row a token's `sid` names, iff it is neither revoked nor expired."""
    row = await session.get(MessagePatientSession, session_id)
    if row is None or not _is_live(row, datetime.now(UTC)):
        return None
    return row


async def revoke_patient_session(session: AsyncSession, row: MessagePatientSession) -> None:
    """Kill one session row (logout). Idempotent — re-revoking keeps the first stamp."""
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await session.commit()


@dataclass(frozen=True)
class PatientSessionRenewal:
    """What `rotate_patient_session` hands back for a cookie it accepted.

    `new_raw_token` is `None` when the presented value was the one a refresh moments ago
    already replaced (inside the grace window): the browser holds the successor from that
    response, and writing another cookie here would only race it.
    """

    row: MessagePatientSession
    account: MessagePatientAccount
    new_raw_token: str | None


async def rotate_patient_session(
    session: AsyncSession, raw_token: str
) -> PatientSessionRenewal | None:
    """Renew the login a cookie names — IN PLACE — or `None` (the caller 401s).

    The row keeps its `id` (every token of the login is bound to it); what rotates is the
    VALUE: a fresh opaque token replaces `token_hash`, the old hash moves to
    `previous_token_hash` with `rotated_at`, and `expires_at` slides forward by
    `PATIENT_SESSION_EXPIRE_DAYS`. Three outcomes for a presented value:

    1. the CURRENT value of a live row → attach the account (a pre-account row adopts its own
       identity here), then rotate by compare-and-swap (`UPDATE ... WHERE token_hash =
       <presented>`, only rowcount 1 counts — `FOR UPDATE` alone was not enough on a
       database that ignores it) and commit ONCE, so nothing commits while the lock is held;
    2. the PREVIOUS value, rotated less than `PATIENT_SESSION_ROTATION_GRACE_SECONDS` ago →
       accept without rotating again (a renewal that was already in flight);
    3. the PREVIOUS value after that window → the reuse/theft signal: every session of the
       ADDRESS is revoked and `None` comes back.
    """
    now = datetime.now(UTC)
    presented = hash_refresh_token(raw_token)
    settings = get_settings()

    row = await session.scalar(
        select(MessagePatientSession)
        .where(MessagePatientSession.token_hash == presented)
        .with_for_update()
    )
    if row is not None:
        if not _is_live(row, now):
            return None
        account = await _row_account(session, row, adopt=True)
        if account is None:
            return None
        new_raw = generate_refresh_token()
        swapped = await session.execute(
            update(MessagePatientSession)
            .where(
                MessagePatientSession.id == row.id,
                MessagePatientSession.token_hash == presented,
            )
            .values(
                token_hash=hash_refresh_token(new_raw),
                previous_token_hash=presented,
                rotated_at=now,
                expires_at=now + timedelta(days=settings.PATIENT_SESSION_EXPIRE_DAYS),
            )
            .execution_options(synchronize_session=False)
        )
        if swapped.rowcount == 1:
            account.last_seen_at = now
            await session.commit()
            await session.refresh(row)
            return PatientSessionRenewal(row=row, account=account, new_raw_token=new_raw)
        # Lost the race to another renewal of this same value: nothing to undo, and no
        # rollback on purpose — the lookup below must see the winner's write.

    row = await session.scalar(
        select(MessagePatientSession)
        .where(MessagePatientSession.previous_token_hash == presented)
        # The loser of the swap above still holds the PRE-rotation instance; deciding
        # grace-vs-theft on those attributes would call the winner a thief.
        .execution_options(populate_existing=True)
    )
    if row is None or not _is_live(row, now):
        return None
    grace = timedelta(seconds=settings.PATIENT_SESSION_ROTATION_GRACE_SECONDS)
    if row.rotated_at is not None and _as_utc(row.rotated_at) + grace > now:
        account = await _row_account(session, row, adopt=True)
        if account is None:
            return None
        await session.commit()
        await session.refresh(row)
        return PatientSessionRenewal(row=row, account=account, new_raw_token=None)

    email = await _row_email(session, row)
    count = await revoke_account_sessions(session, email) if email is not None else 0
    # A count only — never the address, never a token value.
    logger.warning("patient_session_reuse_detected", count=count)
    return None


async def revoke_account_sessions(session: AsyncSession, email: str) -> int:
    """End the whole account of an address: every live session, every clinic, every device.

    Covers the account's rows AND every row still bound to an identity of the address that
    was never adopted — whoever proved that inbox reaches those anyway, and a logout can only
    take access away. `email` must come off an AUTHENTICATED row, never from a request.
    Tokens bound to these rows by `sid` die on their next request. Rows already revoked keep
    their first stamp.
    """
    address = normalize_email(email)
    result = await session.execute(
        update(MessagePatientSession)
        .where(
            MessagePatientSession.revoked_at.is_(None),
            or_(
                MessagePatientSession.account_id.in_(
                    select(MessagePatientAccount.id).where(MessagePatientAccount.email == address)
                ),
                MessagePatientSession.patient_id.in_(
                    select(MessagePatient.id).where(MessagePatient.email == address)
                ),
            ),
        )
        .values(revoked_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount or 0


# --- The PENDING visit: a conversation before any address ---------------------------------
#
# The owner's 2026-09-16 order of events: link -> chat -> e-mail -> LGPD -> real appointment ->
# code. Everything before the code happens under a `MessagePendingSession` and the identity it
# mints. Two rules carry over unchanged from the account model above and are what make this
# safe: (4) the identity's id never changes, and (3) an address grants nothing until a code
# proves it. What is NEW is only that the identity can exist before the address does.


async def open_pending_session(
    session: AsyncSession, tenant: Tenant
) -> tuple[MessagePendingSession, MessagePatient, str]:
    """Mint a handle and a visit for a visitor of `tenant`. Commits.

    Returns `(pending row, identity, RAW opaque token)` — the token exactly once, as every
    credential here is handed out: only its SHA-256 is stored.

    The identity is born with `email = NULL` and `account_id = NULL`, which is precisely what
    makes it invisible to the account machinery (`_adopt`, `add_clinic`, `account_clinics` and
    `revoke_account_sessions` all match on an address, and no NULL satisfies `=` in SQL). Its
    `id` is already the handle secretarIA and PreCheck will store, so the whole conversation —
    including the appointment booked before any code — lives under it from the first message.
    """
    now = datetime.now(UTC)
    patient = MessagePatient(
        id=uuid4(), tenant_id=tenant.id, account_id=None, email=None, last_seen_at=now
    )
    session.add(patient)
    await session.flush()
    raw = generate_refresh_token()
    pending = MessagePendingSession(
        id=uuid4(),
        patient_id=patient.id,
        tenant_id=tenant.id,
        token_hash=hash_refresh_token(raw),
        expires_at=now + timedelta(hours=get_settings().PATIENT_PENDING_EXPIRE_HOURS),
    )
    session.add(pending)
    await session.commit()
    # The clinic only. The handle would line this visit up with the siblings' logs, and there
    # is no address to leak yet precisely because this is what runs before one exists.
    logger.info("patient_pending_session_opened", tenant_id=str(tenant.id))
    return pending, patient, raw


def _pending_is_live(row: MessagePendingSession, now: datetime) -> bool:
    """Live for identity mutation = accessible and not verified yet."""
    return _pending_is_accessible(row, now) and row.verified_at is None


def _pending_is_accessible(row: MessagePendingSession, now: datetime) -> bool:
    """The pending browser may still read/exchange this visit.

    A successful inline code stamps `verified_at` but cannot mint the browser's account
    cookie: the call came from secretarIA over the service leg. The pending bearer therefore
    remains valid until `POST /patient-access/pending/complete` performs that browser-facing
    exchange and stamps `revoked_at`.
    """
    return row.revoked_at is None and _as_utc(row.expires_at) > now


async def find_pending_by_token(
    session: AsyncSession, raw_token: str
) -> MessagePendingSession | None:
    """The accessible visit a raw cookie names, including verified-before-exchange."""
    row = await session.scalar(
        select(MessagePendingSession).where(
            MessagePendingSession.token_hash == hash_refresh_token(raw_token)
        )
    )
    if row is None or not _pending_is_accessible(row, datetime.now(UTC)):
        return None
    return row


async def find_live_pending(
    session: AsyncSession, pending_id: UUID
) -> MessagePendingSession | None:
    """The accessible visit a pending token's `sid` names — re-read on EVERY request.

    The same discipline `find_live_session` applies to a login row: a 30-minute bearer must
    not outlive the row it was minted from (OWASP ASVS 5.0 7.4.1).
    """
    row = await session.get(MessagePendingSession, pending_id)
    if row is None or not _pending_is_accessible(row, datetime.now(UTC)):
        return None
    return row


async def find_pending_identity(
    session: AsyncSession, tenant_id: UUID, patient_ref: UUID
) -> MessagePendingSession | None:
    """The newest accessible visit for this exact clinic handle, or `None`.

    Both keys are required on every service-to-service operation. This is the same tenant
    boundary as `claim_pending_email`, centralized so status/request/verify cannot drift.
    """
    row = await session.scalar(
        select(MessagePendingSession)
        .where(
            MessagePendingSession.patient_id == patient_ref,
            MessagePendingSession.tenant_id == tenant_id,
        )
        .order_by(MessagePendingSession.created_at.desc())
    )
    if row is None or not _pending_is_accessible(row, datetime.now(UTC)):
        return None
    return row


async def claim_pending_email(
    session: AsyncSession, tenant_id: UUID, patient_ref: UUID, email: str
) -> MessagePendingSession | None:
    """Write the address the patient TYPED IN THE CHAT onto their visit. Commits.

    Called service-to-service by secretarIA (`POST /internal/brain-message/pending-email`),
    never by the browser: the whole security argument of the later code is that the address
    being verified is the one the conversation captured, not one a client could name at
    verification time. Re-claiming OVERWRITES (the patient corrected a typo, which is exactly
    what happens in a chat); a code already sent to the previous address simply stops being
    reachable from this visit.

    `None` for a visit that is unknown, dead, or whose clinic/handle disagree with the caller's
    — one answer for every reason, like every refusal in this module.
    """
    now = datetime.now(UTC)
    row = await session.scalar(
        select(MessagePendingSession).where(
            MessagePendingSession.patient_id == patient_ref,
            MessagePendingSession.tenant_id == tenant_id,
        )
    )
    if row is None or not _pending_is_live(row, now):
        return None
    row.email = normalize_email(email)
    row.claimed_at = now
    await session.commit()
    # The clinic and the fact, never the address.
    logger.info("patient_pending_email_claimed", tenant_id=str(tenant_id))
    return row


async def issue_pending_otp(
    session: AsyncSession, pending: MessagePendingSession
) -> str | None:
    """Issue the account challenge for THIS unverified visit and stamp its chat state.

    Returns the raw code only to the caller that immediately hands it to the transactional
    e-mail job. It is never stored or logged. `None` means the visit is no longer mutable or
    has no claimed address.
    """
    if not _pending_is_live(pending, datetime.now(UTC)) or pending.email is None:
        return None
    code = await issue_account_otp(session, pending.email)
    pending.otp_requested_at = datetime.now(UTC)
    await session.commit()
    return code


def pending_otp_is_active(
    pending: MessagePendingSession, now: datetime | None = None
) -> bool:
    """Whether THIS visit's last requested challenge is still inside its TTL."""
    if pending.otp_requested_at is None:
        return False
    current = now or datetime.now(UTC)
    expires_at = _as_utc(pending.otp_requested_at) + timedelta(
        minutes=get_settings().PATIENT_OTP_EXPIRE_MINUTES
    )
    return expires_at > current


@dataclass(frozen=True)
class PendingIdentityVerification:
    account: MessagePatientAccount
    canonical: MessagePatient
    handle_kept: bool


async def verify_pending_identity(
    session: AsyncSession, pending: MessagePendingSession, code: str
) -> PendingIdentityVerification | None:
    """Prove, adopt and link the visit, but leave browser-session issuance for `/complete`.

    This is called by secretarIA over the internal leg, so it must NOT issue a login cookie
    or return an access token. It performs the identity/account work, stamps `verified_at`,
    and deliberately leaves `revoked_at` empty. The browser that already owns the pending
    bearer exchanges it afterwards; no credential ever rides in the chat transcript.
    """
    if not _pending_is_live(pending, datetime.now(UTC)) or pending.email is None:
        return None
    clinic = await channel_open_tenant(session, pending.tenant_id)
    if clinic is None or not await verify_account_otp(session, pending.email, code):
        return None

    account = await open_account(session, pending.email)
    handle_kept = await adopt_pending_identity(session, pending, account.email)
    canonical = await add_clinic(session, account, clinic)
    if canonical is None:  # defensive: the address cannot belong to another account
        return None

    pending.verified_at = datetime.now(UTC)
    if canonical.id != pending.patient_id:
        pending.superseded_by = canonical.id
    await session.commit()
    logger.info(
        "patient_pending_identity_verified",
        tenant_id=str(pending.tenant_id),
        handle_kept=handle_kept,
    )
    return PendingIdentityVerification(
        account=account,
        canonical=canonical,
        handle_kept=handle_kept,
    )


async def complete_pending_identity(
    session: AsyncSession, pending: MessagePendingSession
) -> tuple[MessagePatientAccount, MessagePatient, str, UUID] | None:
    """Atomically exchange a verified visit for the browser's revocable account session.

    The conditional revoke is the one-time claim. The login row and revoke commit together,
    so a failed issuance leaves the pending bearer usable and concurrent completions cannot
    mint two live browser sessions from one visit.
    """
    now = datetime.now(UTC)
    if not _pending_is_accessible(pending, now) or pending.verified_at is None:
        return None
    canonical_id = pending.superseded_by or pending.patient_id
    canonical = await session.get(MessagePatient, canonical_id)
    if canonical is None or canonical.account_id is None:
        return None
    account = await session.get(MessagePatientAccount, canonical.account_id)
    if account is None:
        return None

    claimed = await session.execute(
        update(MessagePendingSession)
        .where(
            MessagePendingSession.id == pending.id,
            MessagePendingSession.revoked_at.is_(None),
            MessagePendingSession.verified_at.is_not(None),
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return None

    raw = generate_refresh_token()
    login = MessagePatientSession(
        id=uuid4(),
        account_id=account.id,
        patient_id=canonical.id,
        tenant_id=canonical.tenant_id,
        token_hash=hash_refresh_token(raw),
        expires_at=now + timedelta(days=get_settings().PATIENT_SESSION_EXPIRE_DAYS),
    )
    session.add(login)
    await session.commit()
    logger.info(
        "patient_pending_identity_completed",
        tenant_id=str(pending.tenant_id),
        handle_kept=pending.superseded_by is None,
    )
    return account, canonical, raw, login.id


async def adopt_pending_identity(
    session: AsyncSession, pending: MessagePendingSession, address: str
) -> bool:
    """Give the visit's identity the address a code just proved. No commit.

    ONE conditional UPDATE, and the condition is the whole design:

        SET email = :address WHERE id = :handle AND email IS NULL
                             AND NOT EXISTS (an identity of this clinic already has :address)

    `email IS NULL` keeps this from ever rewriting an identity that already has an address
    (rule 4: an id and its address are written once). The `NOT EXISTS` is the honest half: a
    clinic may ALREADY have an identity for this address — the same human talked to this same
    clinic before, from an account — and that row owns the `(tenant_id, email)` slot together
    with its own conversation history. Nothing is merged and no id is rewritten; the caller
    falls back to that existing identity and records which one on the visit
    (`close_pending_session`'s `superseded_by`).

    `False` therefore means "this clinic already had one", not "it failed".

    Residual race, stated: two adoptions of the same address at the same clinic could both see
    `NOT EXISTS` and one would then lose the unique key. Unreachable through the only caller —
    `verify_account_otp` burns the single live challenge for an address by compare-and-swap, so
    at most one request per address is ever past it.
    """
    twin = aliased(MessagePatient)
    result = await session.execute(
        update(MessagePatient)
        .where(
            MessagePatient.id == pending.patient_id,
            MessagePatient.email.is_(None),
            ~exists(
                select(literal(1))
                .select_from(twin)
                .where(twin.tenant_id == pending.tenant_id, twin.email == address)
            ),
        )
        .values(email=address, last_seen_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def close_pending_session(
    session: AsyncSession,
    pending: MessagePendingSession,
    *,
    canonical: MessagePatient | None = None,
) -> None:
    """End the visit: it either became an account login, or it was abandoned. Commits.

    `canonical` is the identity the ACCOUNT ended up using at this clinic. When it is not the
    visit's own handle, the clinic already had an identity for the proven address and this
    conversation's handle stays where it is — `superseded_by` records the swap so the answer
    can tell the client which handle to carry on with, and so the trail of "that account
    started in this conversation" survives.
    """
    now = datetime.now(UTC)
    if canonical is not None:
        pending.verified_at = now
        if canonical.id != pending.patient_id:
            pending.superseded_by = canonical.id
    pending.revoked_at = pending.revoked_at or now
    await session.commit()
    logger.info(
        "patient_pending_session_closed",
        tenant_id=str(pending.tenant_id),
        verified=canonical is not None,
        superseded=pending.superseded_by is not None,
    )
