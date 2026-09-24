"""Pydantic v2 schemas for brain-api's INBOUND /internal/brain-message/* surface (Portal).

Service-to-service payloads only (X-Internal-Api-Key callers — today secretarIA).
Nothing here carries a secret, a password hash, or a raw token back out.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# --- The pending visit's e-mail (2026-09-16, chat-first onboarding) ----------------------


class PendingEmailClaimIn(BaseModel):
    """`POST /internal/brain-message/pending-email` — secretarIA captured an address in chat.

    THIS HOP IS WHY THE PATIENT'S OWN CODE IS SAFE. In the owner's flow the visitor types their
    e-mail into the conversation, books a real appointment, and only then receives a code. The
    address must therefore travel from the conversation to brain-api WITHOUT passing through the
    browser: if a client could name the address it wanted verified, holding a pending token would
    be enough to aim a code at anybody's inbox. So it arrives on the SERVICE leg
    (`X-Internal-Api-Key`, the same pair key every other `/internal/*` route uses) and the
    patient-facing `POST /patient-access/pending/request-otp` takes no address at all.

    `external_id` is what secretarIA already holds for this conversation — brain-api's
    `MessagePatient.id`, sent to it as `external_id` on every relayed message
    (`services/message_switchboard.py::send_message`). Named here as the CALLER knows it, not as
    this repo spells it internally (`patient_ref`), so nothing has to be renamed on the wire.

    CLAIMED, NOT PROVEN: this records an address against a conversation and grants nothing. Only
    a verified code moves it onto an identity or into an account.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    external_id: UUID
    # `EmailStr` so a typo the conversation captured fails here, loudly, instead of silently
    # becoming an address no code can ever reach.
    email: EmailStr


class PendingEmailClaimOut(BaseModel):
    """`POST /internal/brain-message/pending-email` response.

    `claimed` is the only success. A conversation that is not a live pending visit — unknown
    handle, wrong clinic, expired, already verified — is a 404 with ONE detail for every
    reason: secretarIA must not be able to tell them apart, and there is nothing it could do
    differently if it could.

    `account_exists`/`email_masked` (2026-09-21) say whether the address just claimed is
    already an account's, so secretarIA can ask the right next question: a name for somebody
    new, the six-digit code for somebody returning. `email_masked` is `core/email_mask.py`'s
    form of that SAME address — first character, `***`, last character, whole domain — and
    is present only alongside `account_exists=True`; the raw value never crosses this
    boundary, here or anywhere else in this file.

    ADDITIVE AND SAFE IN EITHER DEPLOY ORDER, like `PendingOtpRequestOut.email_masked` below:
    both fields carry a default, and today's consumer reads the STATUS CODE only and never
    parses this body (`secretarIA/services/pending_identity.py::claim_email`). A secretarIA
    older than this service therefore keeps working unchanged, and a newer one reads the
    fields best-effort — their absence is an older brain-api, not an error.

    WHY `account_exists` IS NOT AN ENUMERATION LEAK WORTH REFUSING (owner's decision,
    2026-09-20): this is the SERVICE leg, reachable only with `X-Internal-Api-Key`, and the
    one bit it adds is about an address the visitor typed themselves. The mask describes that
    same typed address, so it tells its owner nothing they did not just write. That the
    product then says "your e-mail is already with us" in the chat is the owner's explicit
    decision, recorded in `docs/CHECKPOINT_portal_email_ja_cadastrado.md`.
    """

    status: Literal["claimed"]
    account_exists: bool = False
    email_masked: str | None = None


class PendingIdentityIn(BaseModel):
    """Common key for secretarIA's pending-identity service leg.

    Both fields must match the same visit. `external_id` is the caller's name for
    `MessagePatient.id`; no e-mail or token is accepted on this boundary.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    external_id: UUID


class PendingIdentityStatusOut(BaseModel):
    """PII-free state used before secretarIA decides whether to ask for e-mail."""

    status: Literal[
        "pending_unclaimed",
        "pending_claimed",
        "verified",
        "unknown",
    ]


class PendingOtpRequestOut(BaseModel):
    """The code was accepted for delivery; neither address nor credential leaves.

    `email_masked` (2026-09-19, TASK-003 §3) is the ONE exception, and it is not the address:
    `core/email_mask.py` gives back first character + `***` + last character of the local part
    plus the whole domain, so the notice secretarIA renders can say "digite o código enviado
    para a***a@gmail.com" without the raw value ever crossing this boundary. secretarIA holds
    no copy of the address — that is the whole point of the claim living on the service leg
    (`PendingEmailClaimIn`) — so it cannot compute this itself, and it must not be given the
    address just so it can.

    ADDITIVE AND SAFE IN EITHER DEPLOY ORDER. A caller that ignores the field keeps working;
    a caller that wants it gets it the moment this service is live. It is declared non-optional
    because on a `200` there is always an address to describe — a visit with none is the `409
    pending_email_missing` above, never a `200` with a null.
    """

    status: Literal["sent"]
    email_masked: str


class PendingOtpVerifyIn(PendingIdentityIn):
    """Inline code from secretarIA. The code is consumed in-memory and never logged."""

    code: str = Field(min_length=1, max_length=32)


class PendingOtpVerifyOut(BaseModel):
    """The identity is proven; the browser still has to call `/pending/complete`.

    `patient_name` (2026-09-24, ADDITIVE): the name the proven address's ACCOUNT already gave
    at another clinic, so secretarIA does not ask a known person again. `None` when the
    address has no account yet or the account never gave one. A secretarIA older than this
    reads the status code only and ignores it. PII — never logged on either side.
    """

    status: Literal["verified"]
    patient_name: str | None = None


class PatientNameIn(BaseModel):
    """secretarIA -> brain-api: the name typed into one clinic's conversation.

    Same key as `PendingIdentityIn` — both must match one identity — plus the name as
    secretarIA already normalized it (`secretarIA/services/patient_name.py`). No e-mail, no
    account id: which account it reaches is decided here, by the identity's own link.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    external_id: UUID
    name: str = Field(min_length=1, max_length=255)


class PatientNameOut(BaseModel):
    status: Literal["saved"]
