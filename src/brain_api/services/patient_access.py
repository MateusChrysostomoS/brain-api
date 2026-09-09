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
"""

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.core.security import generate_refresh_token, hash_refresh_token
from brain_api.models import Tenant
from brain_api.models.patient_access import (
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


async def issue_patient_session(session: AsyncSession, patient: MessagePatient) -> str:
    """Create the server-side session row; return the RAW opaque token (once).

    Byte-for-byte the `issue_refresh_token` scheme — high-entropy value out, SHA-256 in —
    because a patient session leaking is the same class of problem as a doctor's, only
    with a different blast radius.
    """
    raw = generate_refresh_token()
    session.add(
        MessagePatientSession(
            patient_id=patient.id,
            tenant_id=patient.tenant_id,
            token_hash=hash_refresh_token(raw),
            expires_at=datetime.now(UTC)
            + timedelta(days=get_settings().PATIENT_SESSION_EXPIRE_DAYS),
        )
    )
    await session.commit()
    return raw


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
