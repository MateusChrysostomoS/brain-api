"""Patient-access models — the PATIENT's end of the Brain-Message channel.

Everything above this line in the repo models a CLINIC's people (`users`, tied to a
tenant, with a role and a password). These tables model the other side: a patient with no
WhatsApp number, who proves an e-mail address and talks to the clinics they chose.

WHY A SEPARATE IDENTITY POPULATION, AND NOT A `User` ROW
`users` is the staff population — every gate in `api/deps.py` reads a role off it, and
`require_doctor`/`require_tenant` would then be one accidental role string away from
letting a patient into the portal. A patient has no role, no password and no portal; the
only thing they can do is talk to the products their clinics bought. Keeping them in their
own tables means no staff gate can ever resolve to a patient, by construction rather than
by review.

THE ACCOUNT IS THE ADDRESS; A CLINIC ENTERS IT ONLY BY THE PATIENT'S GESTURE (2026-09-15)
E-mail + code open a `MessagePatientAccount` — no clinic is needed to log in. A clinic joins
the account only when the patient opens that clinic's link or pastes its link or short code
(`tenants.patient_invite_code`). This REPLACES the 2026-09-12 design, in which a proven
address DISCOVERED every clinic that knew it and the patient confirmed each one by name: no
clinic appears by an e-mail match any more. Membership is `MessagePatient.account_id`; a row
with the same address and no account is invisible to the account until a gesture adds it.

ONE CLINIC IDENTITY PER (tenant, e-mail), AND ITS ID NEVER CHANGES
`MessagePatient.id` is the ONLY handle secretarIA and PreCheck get (`external_id` /
`session_ref`, PreCheck's `bm:<id>`). Adding a clinic REUSES the row that clinic already had
for the address — its conversation history lives under that id — and never merges rows or
hands one clinic's handle to another. It is deliberately NOT reconciled with a WhatsApp
patient who may be the same human: secretarIA looks its patients up by
`(tenant_id, channel, external_id)`, brain-api holds no phone number to link on, and merging
the two would join two consent trails — an LGPD decision nothing in the code asks for.

NOTHING HERE IS EVER STORED IN THE CLEAR EXCEPT THE E-MAIL
The OTP is a credential: only its SHA-256 lands in `code_hash`, the same discipline
`users.reset_token_hash` and `refresh_tokens.token_hash` already follow
(tenant-secrets-encryption: never persist, never log, a secret in plain text). The e-mail is
not a credential — it is the login identifier, exactly like `users.email`.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base

#: The patient opened the Brain-Message channel with this clinic. Recorded ONCE per
#: `MessagePatient`, when that clinic enters the account (a login's invite, the invite route,
#: or the pre-account login). Mirrors secretarIA's `ConsentEvent.kind` convention
#: ("first_contact_service" recorded once at patient creation, never per message).
CONSENT_KIND_CHANNEL_ACCESS = "brain_message_channel_access"

#: HISTORICAL (2026-09-12 → 2026-09-15): the patient confirmed, by name, a clinic discovered
#: by e-mail. Nothing writes it since the invite model; migration 0020 and the adoption of
#: pre-account rows (`services/patient_access.py::_adopt`) read it as proof that the patient
#: chose that clinic.
CONSENT_KIND_ACCOUNT_LINK = "brain_message_account_link"

#: Same placeholder secretarIA records today, and for the same reason: which LGPD basis
#: applies (execução de contrato vs. consentimento) is pending a lawyer's sign-off, and a
#: confidently-wrong basis in an audit trail is worse than an honest marker.
CONSENT_LEGAL_BASIS_PENDING = "TODO_LAWYER"


class MessagePatientAccount(Base):
    """One patient account: the proven address, and nothing else.

    Created by the first verified code for the address (`services/patient_access.py::
    open_account`), with or without a clinic. Its clinics are the `MessagePatient` rows that
    point at it; its logins are the `message_patient_sessions` rows that point at it. No
    name, no phone, no clinic of its own: the address IS the account, so a family sharing an
    inbox shares it (the accepted risk recorded since the 2026-09-12 checkpoint).
    """

    __tablename__ = "message_patient_accounts"
    __table_args__ = (UniqueConstraint("email", name="uq_message_patient_accounts_email"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Lower-cased on write by the service, like `users.email`.
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessagePatientAccountOtp(Base):
    """The pending e-mail challenge for one ADDRESS — at most one, ever.

    The account twin of `MessagePatientOtp`, keyed by the address alone because the code now
    opens the account, not a clinic. Kept out of `message_patient_accounts` for the reason the
    old challenge was kept out of `message_patients`: a code is issued for an address that may
    never verify, and `request-otp` must not mint an account for any address anybody types.

    Re-issuing OVERWRITES the row (one live code at a time) and resets `attempts`.
    """

    __tablename__ = "message_patient_account_otps"
    __table_args__ = (UniqueConstraint("email", name="uq_message_patient_account_otps_email"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    # SHA-256 hex of the code. The code itself exists only in the e-mail that carried it.
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Wrong guesses against THIS challenge: the real ceiling for a 6-digit code, independent
    # of the per-IP limiter an attacker with many IPs would walk around.
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MessagePatient(Base):
    """One patient's stable identity on the Brain-Message channel of ONE clinic.

    `id` IS the wire identifier: it is passed to secretarIA as `external_id` and to PreCheck
    as `session_ref`, as a canonical 36-char lowercase UUID string. That fits both columns
    with room to spare (secretarIA caps `external_id` at 64, PreCheck caps `session_ref` at
    120), and it carries no personal data of its own — which is why the sibling services can
    log it whole.

    Scoped by (tenant_id, email): the SAME human at two clinics is two independent patients
    with two independent handles. The account groups them (`account_id`) and never rewrites
    one or hands one clinic's handle to another.
    """

    __tablename__ = "message_patients"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_message_patients_tenant_email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The account this clinic belongs to, or NULL: a row whose address never made a gesture
    # at this clinic (no code there, no confirmation, no invite) is not the account's, even
    # though the address matches. SET NULL on purpose — the clinic's identity and its
    # conversation handle outlive an account row.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("message_patient_accounts.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # Lower-cased on write by the service, like `users.email`. Indexed (0020): the account's
    # adoption of pre-account rows looks identities up by address across clinics.
    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessagePatientOtp(Base):
    """LEGACY (0018): the per-clinic challenge of the pre-account login.

    Nothing in this brain-api writes or reads it since the account model (2026-09-15); the
    previous deploy still does during the deploy window. Kept mapped so the table's shape is
    not lost before a later migration drops it.
    """

    __tablename__ = "message_patient_otps"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_message_patient_otps_tenant_email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MessagePatientSession(Base):
    """The revocable long leg of a patient session — the sibling of `refresh_tokens`.

    Same scheme, same reasons: an opaque high-entropy value handed to the browser once, only
    its SHA-256 stored, so a DB read never yields a usable credential. The short legs are JWTs
    whose `sid` names THIS row (`core/security.py`), so a logout that kills the row kills
    them on their next request.

    Since 2026-09-15 a login's row belongs to the ACCOUNT (`account_id`): the account token
    and every clinic token of that login name it. `patient_id`/`tenant_id` are only the clinic
    the login was OPENED AT (the login's invite, or the pre-account body's `tenant_id`), kept
    so the transition body of `/refresh` names the same clinic on every renewal — the portal
    deployed on 2026-09-14 drops an account whose login clinic changes. A login opened by
    e-mail alone has both NULL until it has a clinic, then is pinned to its first
    (`services/patient_access.py::login_clinic`). Rows from before the account model carry
    them too, and a row per
    clinic linked by confirmation (never a cookie) may still exist until it expires;
    `api/patient_access.py::_authenticate_patient` accepts both shapes.

    A login's row is RENEWED IN PLACE (`POST /patient-access/refresh`): the opaque value
    rotates and `expires_at` slides, but `id` never changes, because every token of the login
    is bound to it. The staff `refresh_tokens` table rotates by inserting a successor row;
    here that would be a silent logout of every clinic.
    """

    __tablename__ = "message_patient_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("message_patient_accounts.id", ondelete="CASCADE"), index=True, nullable=True
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("message_patients.id", ondelete="CASCADE"), index=True, nullable=True
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # The hash the LAST refresh replaced, and when. The previous value stays answerable for
    # `PATIENT_SESSION_ROTATION_GRACE_SECONDS` so two renewals in flight with the same cookie
    # are not mistaken for theft (`services/patient_access.py::rotate_patient_session`).
    previous_token_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Slid forward by each refresh.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The moment of the CODE; a refresh never moves it.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PatientConsentEvent(Base):
    """LGPD consent/legal-basis audit trail for a Brain-Message patient.

    Shaped after secretarIA's `consent_events` deliberately — same columns, same semantics,
    same `legal_basis` placeholder — so the two channels' trails can be read (and, later,
    erased) by one process rather than two dialects. The one rename is secretarIA's `wa_id`,
    whose name already lies over there: it holds a `channel="brain_message"` patient's
    `external_id` too. Here the field is born with the honest name and the SAME `String(64)`
    width, which is what makes the two interchangeable.

    `CONSENT_KIND_CHANNEL_ACCESS` is recorded exactly ONCE per `MessagePatient`, when that
    clinic enters the account — never per message, never per login, never per invite replay.
    """

    __tablename__ = "patient_consent_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # `MessagePatient.id` as a string — the same subject handle the sibling services see.
    subject_ref: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    legal_basis: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
