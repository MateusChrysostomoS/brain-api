"""Patient-access service — the OTP challenge, the identity it mints, the session.

The three things this module owns, and the rule each one follows:

1. THE CODE IS A CREDENTIAL. It is generated with `secrets`, hashed with SHA-256 before
   it touches the database, compared in constant time, and never logged — the same
   discipline `services/auth.py` applies to reset and refresh tokens, and what the
   tenant-secrets-encryption skill asks of anything secret-shaped. The ONE place the
   plaintext exists after generation is the e-mail body; the router hands it straight to
   the mail delegate and keeps no other reference.

2. NOTHING HERE DISTINGUISHES AN UNKNOWN SUBJECT. Every helper returns `None` for an
   unknown tenant, an unknown address, a wrong code and an expired code alike. Telling
   those apart is how a login form becomes a patient-list oracle for whoever can type an
   e-mail. The ROUTER answers identically for all of them — do not "improve" these
   helpers by raising a specific error, exactly as the password-reset helpers warn.

3. THE TENANT IS THE OUTER SCOPE OF EVERY QUERY. Not a filter applied afterwards: the
   `(tenant_id, email)` pair is the key of both the challenge and the identity, so the
   same human writing to two clinics is two rows that cannot see each other.

   EXACTLY TWO DELIBERATE EXCEPTIONS, both added for the multi-clinic account
   (2026-09-12), and both keyed by the address on a row the caller already PROVED —
   never by an address or a tenant taken from a request: `find_sibling_candidates`
   (discovery: reads, mints nothing) and `revoke_account_sessions` (logout: can only
   take access away). The step that turns a discovered row into access,
   `confirm_sibling_link`, is back inside one tenant. See the
   cross-tenant-account-linking skill before adding a third.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.core.security import generate_refresh_token, hash_refresh_token
from brain_api.models import Tenant
from brain_api.models.patient_access import (
    CONSENT_KIND_ACCOUNT_LINK,
    CONSENT_KIND_CHANNEL_ACCESS,
    CONSENT_LEGAL_BASIS_PENDING,
    MessagePatient,
    MessagePatientOtp,
    MessagePatientSession,
    PatientConsentEvent,
)

logger = get_logger(__name__)


def normalize_email(raw: str) -> str:
    """The one spelling an address is stored and looked up under (`users.email`'s rule)."""
    return raw.strip().lower()


def generate_otp_code() -> str:
    """A zero-padded decimal code of `PATIENT_OTP_LENGTH` digits, from `secrets`.

    Zero-padding matters: dropping a leading zero would shrink the space AND produce a
    code the patient cannot retype as shown. `randbelow` over the full range keeps every
    value equally likely (a `%`-based fold would not).
    """
    digits = max(4, get_settings().PATIENT_OTP_LENGTH)
    return f"{secrets.randbelow(10**digits):0{digits}d}"


def _hash_code(code: str) -> str:
    """SHA-256 hex, the storage form. Fast is correct here for the same reason it is for
    refresh tokens — but NOT for the same reason: a 6-digit code IS guessable, so what
    protects it is `PATIENT_OTP_MAX_ATTEMPTS` plus the short expiry, never the hash's
    cost. Stretching would only slow the honest patient down."""
    return hash_refresh_token(code)


def _as_utc(dt: datetime) -> datetime:
    """Normalize a DB datetime for comparison (SQLite returns naive; Postgres aware)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def channel_open_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant | None:
    """The tenant, iff it exists AND has the Brain-Message channel switched on.

    Fail CLOSED at the front door: a clinic that never enabled the channel must not be
    able to receive a patient login at all, let alone one that later gets an empty thread
    list. `tenants.brain_message_enabled` is the same flag `GET /entitlements` projects as
    `channels.brain_message`, so the login gate and the thread gate cannot drift apart.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or not tenant.brain_message_enabled:
        return None
    return tenant


async def issue_otp(session: AsyncSession, tenant_id: UUID, email: str) -> str | None:
    """Mint a code for `(tenant_id, email)`, persisting only its hash.

    Returns the RAW code so the caller can e-mail it, or `None` when the tenant does not
    exist or has the channel off — the caller must answer identically either way.

    Issuing OVERWRITES any live challenge for the same pair and resets `attempts`: one
    code at a time, and a patient who asks for a fresh one is not punished for the
    guesses spent on the old one. (The reset is also why re-requesting cannot be used to
    farm attempts: the row that is reset is the row that is replaced.)
    """
    if await channel_open_tenant(session, tenant_id) is None:
        return None

    address = normalize_email(email)
    code = generate_otp_code()
    expires_at = datetime.now(UTC) + timedelta(
        minutes=get_settings().PATIENT_OTP_EXPIRE_MINUTES
    )

    challenge = await session.scalar(
        select(MessagePatientOtp).where(
            MessagePatientOtp.tenant_id == tenant_id,
            MessagePatientOtp.email == address,
        )
    )
    if challenge is None:
        challenge = MessagePatientOtp(tenant_id=tenant_id, email=address, code_hash="")
        session.add(challenge)
    challenge.code_hash = _hash_code(code)
    challenge.expires_at = expires_at
    challenge.attempts = 0
    challenge.consumed_at = None
    await session.commit()

    # Tenant only. The address is personal data and the code is a credential; neither
    # belongs in a log line, and together they would be a working login.
    logger.info("patient_otp_issued", tenant_id=str(tenant_id))
    return code


async def verify_otp(
    session: AsyncSession, tenant_id: UUID, email: str, code: str
) -> MessagePatient | None:
    """Consume a code and return the patient identity it unlocks, or `None`.

    `None` covers every failure — unknown tenant, channel off, no challenge, wrong code,
    expired, already consumed, out of attempts — because the caller must not be able to
    tell them apart.

    The comparison is `secrets.compare_digest` over the two HASHES. Comparing hashes
    rather than codes is what keeps the timing signal independent of how many leading
    digits happened to match, and `compare_digest` removes what is left.

    A wrong guess costs an attempt and is committed even though the verification failed:
    a counter that only persists on success counts nothing.
    """
    if await channel_open_tenant(session, tenant_id) is None:
        return None

    address = normalize_email(email)
    challenge = await session.scalar(
        select(MessagePatientOtp).where(
            MessagePatientOtp.tenant_id == tenant_id,
            MessagePatientOtp.email == address,
        )
    )
    if challenge is None or challenge.consumed_at is not None:
        return None
    if _as_utc(challenge.expires_at) <= datetime.now(UTC):
        return None
    if challenge.attempts >= get_settings().PATIENT_OTP_MAX_ATTEMPTS:
        return None

    if not secrets.compare_digest(challenge.code_hash, _hash_code(code)):
        challenge.attempts += 1
        await session.commit()
        logger.info("patient_otp_rejected", tenant_id=str(tenant_id))
        return None

    challenge.consumed_at = datetime.now(UTC)
    patient = await _resolve_patient(session, tenant_id, address)
    await session.commit()
    logger.info(
        "patient_otp_verified", tenant_id=str(tenant_id), patient_ref=str(patient.id)
    )
    return patient


async def _resolve_patient(
    session: AsyncSession, tenant_id: UUID, address: str
) -> MessagePatient:
    """Get-or-create the identity for `(tenant_id, address)` — and, on create, its consent
    row. Does NOT commit; `verify_otp` owns the single commit that also burns the code.

    A returning patient keeps the SAME `id`, which is what makes their secretarIA
    conversation and their PreCheck session survive a re-login: both services key off
    that string, so a fresh id would silently start a brand-new conversation and lose the
    history the patient can still see on their screen.

    The consent row is written in the same breath as the identity, and only then, which
    is what makes "recorded exactly once, at patient creation" true rather than
    aspirational (secretarIA's own "first_contact_service" rule).
    """
    patient = await session.scalar(
        select(MessagePatient).where(
            MessagePatient.tenant_id == tenant_id,
            MessagePatient.email == address,
        )
    )
    now = datetime.now(UTC)
    if patient is not None:
        patient.last_seen_at = now
        return patient

    patient = MessagePatient(tenant_id=tenant_id, email=address, last_seen_at=now)
    session.add(patient)
    # `flush` (not `commit`) so `patient.id` exists for the consent row's subject_ref
    # while both stay inside the caller's single transaction.
    await session.flush()
    session.add(
        PatientConsentEvent(
            tenant_id=tenant_id,
            subject_ref=str(patient.id),
            kind=CONSENT_KIND_CHANNEL_ACCESS,
            legal_basis=CONSENT_LEGAL_BASIS_PENDING,
        )
    )
    logger.info(
        "patient_consent_recorded",
        tenant_id=str(tenant_id),
        patient_ref=str(patient.id),
        kind=CONSENT_KIND_CHANNEL_ACCESS,
    )
    return patient


# --- The session (revocable leg) -------------------------------------------------------


async def issue_patient_session(
    session: AsyncSession, patient: MessagePatient, *, lifetime: timedelta | None = None
) -> tuple[str, UUID]:
    """Create the server-side session row; return the RAW opaque token (once) and the row id.

    Byte-for-byte the `issue_refresh_token` scheme — high-entropy value out, SHA-256 in —
    because a patient session leaking is the same class of problem as a doctor's, only
    with a different blast radius.

    The id comes back because the access JWT carries it as `sid`
    (`core/security.py::create_patient_token`), which is what lets revoking this row
    refuse that JWT too. It is minted here, not read back after the commit, so no caller
    ever depends on a post-commit refresh.

    `lifetime` defaults to `PATIENT_SESSION_EXPIRE_DAYS`, a login's. A clinic linked by
    confirmation passes the access token's lifetime instead: its row is never presented
    as a cookie, so it only has to outlive the one JWT that names it.
    """
    raw = generate_refresh_token()
    row = MessagePatientSession(
        id=uuid4(),
        patient_id=patient.id,
        tenant_id=patient.tenant_id,
        token_hash=hash_refresh_token(raw),
        expires_at=datetime.now(UTC)
        + (
            lifetime
            if lifetime is not None
            else timedelta(days=get_settings().PATIENT_SESSION_EXPIRE_DAYS)
        ),
    )
    session.add(row)
    session_id = row.id
    await session.commit()
    return raw, session_id


async def find_patient_session(
    session: AsyncSession, raw_token: str
) -> tuple[MessagePatientSession, MessagePatient] | None:
    """Resolve a raw session token to its live row + patient, or `None`.

    `None` for unknown, expired and revoked alike. The patient is loaded in the same call
    because every caller needs the tenant off it, and a second lookup is a second place
    the scope could be forgotten.
    """
    row = await session.scalar(
        select(MessagePatientSession).where(
            MessagePatientSession.token_hash == hash_refresh_token(raw_token)
        )
    )
    if row is None or row.revoked_at is not None:
        return None
    if _as_utc(row.expires_at) <= datetime.now(UTC):
        return None
    patient = await session.get(MessagePatient, row.patient_id)
    if patient is None or patient.tenant_id != row.tenant_id:
        # Belt and braces: the denormalized scope disagreeing with the identity's own
        # tenant means something rewrote one of them. Refuse rather than pick a side.
        return None
    return row, patient


async def revoke_patient_session(session: AsyncSession, row: MessagePatientSession) -> None:
    """Kill one session row (logout). Idempotent — re-revoking keeps the first stamp."""
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await session.commit()


async def find_live_session(
    session: AsyncSession, session_id: UUID
) -> MessagePatientSession | None:
    """The row an access token's `sid` names, iff it is neither revoked nor expired.

    The bearer-side twin of `find_patient_session` (which starts from the raw cookie
    value instead). `None` for unknown, revoked and expired alike.
    """
    row = await session.get(MessagePatientSession, session_id)
    if row is None or row.revoked_at is not None:
        return None
    if _as_utc(row.expires_at) <= datetime.now(UTC):
        return None
    return row


def session_is_recent(row: MessagePatientSession, minutes: int) -> bool:
    """True while `row` — opened by a verified code — is younger than `minutes`.

    Measures `created_at`, the moment of the CODE, and a refresh never moves it
    (`rotate_patient_session`). So on a long, renewed session this goes false and stays
    false: confirming a clinic for the FIRST time then needs a fresh code, while clinics
    already linked keep reopening through the refresh. That split is the decision, not an
    oversight — see `config.py::PATIENT_LINK_CONFIRM_WINDOW_MINUTES`.
    """
    return _as_utc(row.created_at) + timedelta(minutes=minutes) > datetime.now(UTC)


@dataclass(frozen=True)
class PatientSessionRenewal:
    """What `rotate_patient_session` hands back for a cookie it accepted.

    `new_raw_token` is `None` when the presented value was the one a refresh moments ago
    already replaced (inside the grace window): the browser is holding the successor
    from that response, and writing another cookie here would only race it.
    """

    row: MessagePatientSession
    patient: MessagePatient
    new_raw_token: str | None


async def _live_row_patient(
    session: AsyncSession, row: MessagePatientSession, now: datetime
) -> MessagePatient | None:
    """The patient behind a row that is neither revoked nor expired, else `None`."""
    if row.revoked_at is not None or _as_utc(row.expires_at) <= now:
        return None
    patient = await session.get(MessagePatient, row.patient_id)
    if patient is None or patient.tenant_id != row.tenant_id:
        return None
    return patient


async def rotate_patient_session(
    session: AsyncSession, raw_token: str
) -> PatientSessionRenewal | None:
    """Renew the login session a cookie names — IN PLACE — or `None` (the caller 401s).

    The staff twin, `services/auth.py::rotate_refresh_token`, inserts a successor row and
    revokes the presented one. That cannot be copied here: `MessagePatientSession.id` is
    the `sid` of the login's access token AND the `login_sid` of every linked clinic's
    token (`api/patient_access.py::_authenticate_patient` checks both against a LIVE row),
    so a new row per refresh would sign the patient out of every other clinic on the very
    request meant to keep them in. So the row keeps its `id`; what rotates is the VALUE:
    a fresh opaque token replaces `token_hash`, the old hash moves to
    `previous_token_hash` with `rotated_at`, and `expires_at` slides forward by
    `PATIENT_SESSION_EXPIRE_DAYS` — the sliding renewal. `created_at` is left alone on
    purpose (`session_is_recent`).

    Three outcomes for a presented value:

    1. It is the CURRENT value of a live row → rotate as above, return the new raw token.
       The rotation is a compare-and-swap: `UPDATE ... WHERE token_hash = <presented>`,
       and only a rowcount of 1 counts. Two requests carrying the same value can both
       READ the row before either writes; only one UPDATE can match, and the other falls
       through to case 2 instead of rotating a second time and leaving the browser with a
       cookie the DB never kept. (The select also takes `FOR UPDATE`, which on Postgres
       makes the second request wait and re-read; the CAS is what makes the outcome the
       same on a database that ignores the lock, SQLite included — the suite proved the
       lock alone was not enough.)
    2. It is the PREVIOUS value and the rotation is younger than
       `PATIENT_SESSION_ROTATION_GRACE_SECONDS` → accept: a renewal that was already in
       flight when another one landed (the portal polls several threads and clinics at
       once). Returns the row with `new_raw_token=None`: no second rotation, no cookie.
    3. It is the PREVIOUS value and the window has closed → the reuse/theft signal, the
       staff route's semantics: a value this browser was told to discard is being
       presented again from somewhere. Every live session of the ACCOUNT is revoked
       (`revoke_account_sessions` — the address is the account), a warning is logged with
       ids only, and `None` comes back. The legitimate patient re-proves the inbox.

    Unknown, revoked and expired values are `None` alike; nothing here distinguishes them.
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
        patient = await _live_row_patient(session, row, now)
        if patient is None:
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
            # The patient is back — the fact `_resolve_patient` stamps on a login.
            patient.last_seen_at = now
            await session.commit()
            await session.refresh(row)
            return PatientSessionRenewal(row=row, patient=patient, new_raw_token=new_raw)
        # Lost the race: another renewal rotated this same value between our read and
        # our write. The UPDATE matched nothing, so there is nothing to undo — and no
        # rollback on purpose: `presented` is now the row's PREVIOUS value, and the lookup
        # below must see the winner's write, committed or (under a shared connection)
        # still in flight.

    row = await session.scalar(
        select(MessagePatientSession)
        .where(MessagePatientSession.previous_token_hash == presented)
        # Re-read the columns even if this session already holds the instance: a request
        # that lost the compare-and-swap above still has the PRE-rotation attributes in its
        # identity map, and deciding grace-vs-theft on those would call the winner a thief.
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    patient = await _live_row_patient(session, row, now)
    if patient is None:
        return None
    grace = timedelta(seconds=settings.PATIENT_SESSION_ROTATION_GRACE_SECONDS)
    if row.rotated_at is not None and _as_utc(row.rotated_at) + grace > now:
        return PatientSessionRenewal(row=row, patient=patient, new_raw_token=None)

    count = await revoke_account_sessions(session, patient.email)
    # Tenant and a count only — never the address, never a token value.
    logger.warning(
        "patient_session_reuse_detected",
        tenant_id=str(patient.tenant_id),
        count=count,
    )
    return None


async def reopen_linked_clinic(session: AsyncSession, sibling: MessagePatient) -> UUID:
    """A fresh session row for a clinic the account ALREADY linked; returns its id.

    The refresh's answer to the multi-clinic account: a patient who confirmed a sibling
    clinic must not be asked to confirm it again — or type a code for it — just because
    the portal was reopened. The authority is the `CONSENT_KIND_ACCOUNT_LINK` event that
    confirmation recorded; the CALLER must have checked it (`linked_identities`) — this
    helper mints, it does not decide. No recency gate, because nothing new is being
    granted: the clinic was already reachable by this account.

    Same row `confirm_sibling` issues — the access token's lifetime, no cookie, so the
    row is only the revocation handle its JWT's `sid` points at — and the same
    `last_seen_at` stamp.
    """
    sibling.last_seen_at = datetime.now(UTC)
    _, session_id = await issue_patient_session(
        session,
        sibling,
        lifetime=timedelta(minutes=get_settings().PATIENT_TOKEN_EXPIRE_MINUTES),
    )
    return session_id


# --- The multi-clinic account (discover, confirm by name, end it whole) ----------------


async def find_sibling_candidates(
    session: AsyncSession, email: str, exclude_tenant_id: UUID
) -> list[tuple[MessagePatient, Tenant]]:
    """Every OTHER clinic where this address is already a Brain-Message patient.

    DISCOVERY ONLY — a pure read that mints no session, no token and no identity. The
    address is the one attribute two clinics share, and it cannot by itself authorize a
    link: it can be shared by a family or reassigned to someone else (OpenID Connect Core
    §5.7 says as much of the `email` claim). So this returns QUESTIONS for the patient,
    never grants; the grant is `confirm_sibling_link`, one named clinic at a time.

    Only identities that already exist come back — a `MessagePatient` row is created by a
    verified code and nothing else — so the account can rediscover a clinic the address
    once proved itself at, never invent one. The channel gate is `channel_open_tenant`'s,
    applied in the same query: a clinic that switched Brain-Message off is not offered.

    `email` must come off a row the patient already proved (the authenticated
    `MessagePatient.email`), never from a request. Ordered by clinic name so the client
    shows the same list every time.
    """
    rows = await session.execute(
        select(MessagePatient, Tenant)
        .join(Tenant, Tenant.id == MessagePatient.tenant_id)
        .where(
            MessagePatient.email == normalize_email(email),
            MessagePatient.tenant_id != exclude_tenant_id,
            Tenant.brain_message_enabled.is_(True),
        )
        .order_by(Tenant.clinic_name, Tenant.id)
    )
    return [(patient, tenant) for patient, tenant in rows.all()]


async def linked_identities(session: AsyncSession, patients: list[MessagePatient]) -> set[UUID]:
    """Which of these identities the account already linked by an explicit confirmation.

    One query for the whole candidate list. A link counts only under the identity AND its
    own tenant — the pair the consent row is written with — so an event that somehow
    disagreed with its identity's tenant would not read as consent.
    """
    if not patients:
        return set()
    rows = await session.execute(
        select(PatientConsentEvent.tenant_id, PatientConsentEvent.subject_ref).where(
            PatientConsentEvent.kind == CONSENT_KIND_ACCOUNT_LINK,
            PatientConsentEvent.subject_ref.in_([str(p.id) for p in patients]),
        )
    )
    recorded = {(tenant_id, subject_ref) for tenant_id, subject_ref in rows.all()}
    return {p.id for p in patients if (p.tenant_id, str(p.id)) in recorded}


async def confirm_sibling_link(
    session: AsyncSession, patient: MessagePatient, tenant_id: UUID
) -> tuple[MessagePatient, Tenant] | None:
    """Link clinic `tenant_id` to the account `patient` is logged into — by consent.

    Resolves the SAME address's identity at that clinic, scoped to that one tenant AND to
    the address on the authenticated row, so no input reaches anybody else's identity;
    then records the patient's explicit confirmation as a `CONSENT_KIND_ACCOUNT_LINK`
    event on THAT clinic's tenant.

    `None` for the patient's own clinic, an unknown clinic, a clinic with the channel off
    and a clinic where the address never verified, alike: the router answers one 404 for
    all of them, so the route is no oracle for which clinics know an address.

    Idempotent: a second confirmation records nothing, and the row lock serializes two
    concurrent ones on Postgres (SQLite ignores `FOR UPDATE`; the suite is sequential), so
    a double tap cannot write two events. Commits BEFORE the caller issues the session —
    `verify-otp`'s order: the consent trail must not depend on the session insert.
    """
    if tenant_id == patient.tenant_id:
        return None
    found = (
        await session.execute(
            select(MessagePatient, Tenant)
            .join(Tenant, Tenant.id == MessagePatient.tenant_id)
            .where(
                MessagePatient.tenant_id == tenant_id,
                MessagePatient.email == patient.email,
                Tenant.brain_message_enabled.is_(True),
            )
            .with_for_update(of=MessagePatient)
        )
    ).first()
    if found is None:
        return None
    sibling, tenant = found

    already = await session.scalar(
        select(PatientConsentEvent.id)
        .where(
            PatientConsentEvent.tenant_id == sibling.tenant_id,
            PatientConsentEvent.subject_ref == str(sibling.id),
            PatientConsentEvent.kind == CONSENT_KIND_ACCOUNT_LINK,
        )
        .limit(1)
    )
    if already is None:
        session.add(
            PatientConsentEvent(
                tenant_id=sibling.tenant_id,
                subject_ref=str(sibling.id),
                kind=CONSENT_KIND_ACCOUNT_LINK,
                legal_basis=CONSENT_LEGAL_BASIS_PENDING,
            )
        )
    # The patient is back at this clinic, without a code — the fact `_resolve_patient`
    # stamps on a login.
    sibling.last_seen_at = datetime.now(UTC)
    await session.commit()
    # Tenant and a count only: never the address, never the clinic's name, and not the
    # identity handle either — one line carrying two tenants' handles for the same human
    # is exactly the cross-tenant join the per-clinic rows exist to avoid.
    logger.info(
        "patient_account_link_confirmed",
        tenant_id=str(sibling.tenant_id),
        consent_events_recorded=0 if already is not None else 1,
    )
    return sibling, tenant


async def revoke_account_sessions(session: AsyncSession, email: str) -> int:
    """End the whole account: every live session of the address, at every clinic, on
    every device. Returns how many rows it revoked.

    Joined by ADDRESS, never by tenant: the account IS the address, so ending it must
    reach the clinics the patient linked AND any other clinic that address signed into —
    a live token for this account could open those anyway. It can only take access away.
    Tokens bound to these rows by `sid` die on their next request
    (`api/patient_access.py::get_current_patient`).

    Rows already revoked keep their first stamp (`revoked_at IS NULL`), the idempotency
    `revoke_patient_session` promises for one row. `email` must come off an AUTHENTICATED
    patient row, never from a request.
    """
    result = await session.execute(
        update(MessagePatientSession)
        .where(
            MessagePatientSession.revoked_at.is_(None),
            MessagePatientSession.patient_id.in_(
                select(MessagePatient.id).where(MessagePatient.email == normalize_email(email))
            ),
        )
        .values(revoked_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount or 0
