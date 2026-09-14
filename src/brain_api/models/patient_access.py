"""Patient-access models — the PATIENT's end of the Brain-Message channel.

Everything above this line in the repo models a CLINIC's people (`users`, tied to a
tenant, with a role and a password). These four tables model the other side: a patient
with no WhatsApp number, who proves an e-mail address and gets a session that can reach
exactly one clinic.

WHY A SEPARATE IDENTITY POPULATION, AND NOT A `User` ROW
`users` is the staff population — every gate in `api/deps.py` reads a role off it, and
`require_doctor`/`require_tenant` would then be one accidental role string away from
letting a patient into the portal. A patient has no role, no password and no portal; the
only thing they can do is talk to the products their clinic bought. Keeping them in their
own table means no staff gate can ever resolve to a patient, by construction rather than
by review.

THE IDENTITY IS NEW, NOT LINKED TO A WHATSAPP PATIENT
`MessagePatient.id` is minted here and is the ONLY handle secretarIA and PreCheck get
(their `external_id` / `session_ref`). It is deliberately NOT reconciled with a WhatsApp
patient who may be the same human: secretarIA looks its patients up by
`(tenant_id, channel, external_id)` and its Brain-Message read path filters
`channel == "brain_message"` explicitly — "a WhatsApp patient who happens to share the
string is not reachable here" (secretaria api/internal.py). brain-api holds no phone
number for a patient at all, so there is no attribute to link on even if we wanted to.
Merging the two identities is a product decision with an LGPD dimension (it joins two
consent trails), and nothing in the code today asks for it.

ONE ACCOUNT, MANY CLINICS — LINKED BY CONSENT, NEVER MERGED (2026-09-12)
The owner did ask for the Brain-Message-to-Brain-Message version of that join: whoever
proved an address at one clinic sees every clinic that knows the address, without a new
code per clinic. No row below changed for it. Each clinic keeps its own `MessagePatient`
(its own handle, its own consent trail); what joins them is an explicit, named
confirmation recorded as a `CONSENT_KIND_ACCOUNT_LINK` event on the linked clinic, plus a
session of its own there. The address alone never links anything — it can be shared by a
family or reassigned to someone else (OpenID Connect Core §5.7 says the same of the
`email` claim) — so a match only DISCOVERS a candidate the patient then accepts or not
(`services/patient_access.py::find_sibling_candidates` / `confirm_sibling_link`).

NOTHING HERE IS EVER STORED IN THE CLEAR EXCEPT THE E-MAIL
The OTP is a credential: only its SHA-256 lands in `code_hash`, the same discipline
`users.reset_token_hash` and `refresh_tokens.token_hash` already follow
(tenant-secrets-encryption: never persist, never log, a secret in plain text). The
e-mail is not a credential — it is the login identifier, exactly like `users.email`.
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

#: The one consent kind this round records: the patient opened the Brain-Message channel
#: with this clinic. Mirrors secretarIA's `ConsentEvent.kind` convention
#: ("first_contact_service" recorded ONCE at patient creation, never per message).
CONSENT_KIND_CHANNEL_ACCESS = "brain_message_channel_access"

#: The patient, logged in at ANOTHER clinic with the same e-mail, explicitly confirmed that
#: THIS clinic's conversation is theirs (the multi-clinic account, 2026-09-12). Recorded
#: once per linked identity, on the LINKED clinic's tenant, and never inferred from the
#: address alone — see `services/patient_access.py::confirm_sibling_link`.
CONSENT_KIND_ACCOUNT_LINK = "brain_message_account_link"

#: Same placeholder secretarIA records today, and for the same reason: which LGPD basis
#: applies (execução de contrato vs. consentimento) is pending a lawyer's sign-off, and a
#: confidently-wrong basis in an audit trail is worse than an honest marker.
CONSENT_LEGAL_BASIS_PENDING = "TODO_LAWYER"


class MessagePatient(Base):
    """One patient's stable identity on the Brain-Message channel of ONE clinic.

    `id` IS the wire identifier: it is passed to secretarIA as `external_id` and to
    PreCheck as `session_ref`, as a canonical 36-char lowercase UUID string. That fits
    both columns with room to spare (secretarIA caps `external_id` at 64, PreCheck caps
    `session_ref` at 120), and it carries no personal data of its own — which is why the
    sibling services can log it whole.

    Scoped by (tenant_id, email): the SAME human mailing two different clinics is two
    independent patients with two independent handles. That is the tenant isolation
    boundary in the data model, before any query ever runs — and the multi-clinic account
    keeps it: linking adds a consent event and a session, it never rewrites a row or
    hands one clinic's handle to another.
    """

    __tablename__ = "message_patients"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_message_patients_tenant_email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Lower-cased on write by the service, like `users.email`.
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessagePatientOtp(Base):
    """The pending e-mail challenge for one (tenant, e-mail) pair — at most one, ever.

    Kept OUT of `message_patients` on purpose: a challenge is issued for an address that
    may never verify, and folding it into the identity table would mean `request-otp`
    mints a patient row for any address anybody types. Here an unverified address leaves
    a sweepable challenge and no identity.

    Re-issuing OVERWRITES the row (one live code at a time), the same rule
    `issue_password_reset_token` already applies to reset links.
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
    # SHA-256 hex of the code. The code itself exists only in the e-mail that carried it.
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Wrong guesses against THIS challenge. A short code needs a ceiling that does not
    # depend on the per-IP limiter: an attacker with many IPs would otherwise brute-force
    # 10^6 codes against one long-lived challenge.
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MessagePatientSession(Base):
    """The revocable long leg of a patient session — the sibling of `refresh_tokens`.

    Same scheme, same reasons: an opaque high-entropy value handed to the browser once,
    only its SHA-256 stored, so a DB read never yields a usable credential. The short leg
    is a scoped JWT (`core/security.py::create_patient_token`) whose `sid` claim names
    THIS row, so a logout that kills the row kills the JWT on its next request too.

    A clinic linked by confirmation (`services/patient_access.py::confirm_sibling_link`)
    gets a row of its own whose opaque token is never handed out — the cookie is flat and
    stays the login session's — so for that session the row is purely the revocation
    handle its JWT's `sid` points at.

    A login's row is RENEWED IN PLACE (2026-09-14, `POST /patient-access/refresh`): the
    opaque value rotates and `expires_at` slides, but `id` never changes, because that id
    is what every token of the account — the login's `sid`, each linked clinic's
    `login_sid` — is bound to. The staff `refresh_tokens` table rotates by inserting a
    successor row; here that would be a silent logout of every linked clinic.

    `tenant_id` is denormalized off the patient on purpose: every lookup in this vertical
    is "this session, for this tenant", and carrying the scope on the row means the
    tenant check sits in the same SQL as the token check rather than in a second
    round-trip that a future refactor could quietly drop.
    """

    __tablename__ = "message_patient_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("message_patients.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # The hash the LAST refresh replaced, and when. A login's row keeps its `id` across
    # renewals — every access token and every linked clinic's token names it by `sid` /
    # `login_sid`, so a new row per refresh would sign the patient out of everything
    # (`services/patient_access.py::rotate_patient_session`). Only the VALUE rotates, and
    # the previous one stays answerable for `PATIENT_SESSION_ROTATION_GRACE_SECONDS` so
    # two renewals in flight with the same cookie are not mistaken for theft. NULL until
    # the first refresh; a row that was never presented as a cookie (a linked clinic's)
    # never fills it.
    previous_token_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Slid forward by each refresh (a login's row) — never for a linked clinic's row, which
    # lives exactly as long as the one JWT that names it.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The moment of the CODE. Deliberately untouched by a refresh: `session_is_recent`
    # reads it to decide whether a FIRST link to another clinic still has a fresh proof.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PatientConsentEvent(Base):
    """LGPD consent/legal-basis audit trail for a Brain-Message patient.

    Shaped after secretarIA's `consent_events` deliberately — same columns, same
    semantics, same `legal_basis` placeholder — so the two channels' trails can be read
    (and, later, erased) by one process rather than two dialects. The one rename is
    secretarIA's `wa_id`, whose name already lies over there: it holds a
    `channel="brain_message"` patient's `external_id` too. Here the field is born with
    the honest name and the SAME `String(64)` width, which is what makes the two
    interchangeable.

    Recorded exactly ONCE per patient, at the moment the identity is first minted
    (`verify-otp`), never per message — secretarIA's "first_contact_service" rule.
    `CONSENT_KIND_ACCOUNT_LINK` follows the same once-only rule per linked identity: a
    second confirmation of the same clinic records nothing new.
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
