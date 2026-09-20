"""Patient-access router — the ONLY browser-facing surface a patient can reach.

THE TRUST BOUNDARY, SPELLED OUT ONCE AND ENFORCED IN CODE (auth-jwt-multitenant):

  browser (patient)  --->  brain-api      : `Authorization: Bearer <scoped patient JWT>`
                                            + `__Host-patient_session` cookie
  brain-api          --->  secretarIA     : `X-Internal-Api-Key`  (service-pair secret)
  brain-api          --->  PreCheck       : `X-Internal-Token`    (service-pair secret)

The two internal keys are SYMMETRIC secrets shared by exactly two services and must NEVER
authenticate an end user. The patient's own leg uses purpose-scoped JWTs instead, and
`api/deps.py::get_current_principal` rejects ANY token carrying a `scope`, so neither kind
below can open a staff route.

THREE PATIENT TOKENS, EACH NARROWER THAN THE LAST:
- the ACCOUNT token (`scope=patient_account`, since 2026-09-15): e-mail + code open the
  account; this token adds clinics by invite (`POST /clinics`) and ends the account. It opens
  no thread.
- one CLINIC token per clinic of the account (`scope=patient_message`, one tenant): the
  everyday credential of the thread routes.
- the PENDING token (`scope=patient_pending`, since 2026-09-16): a visitor who has proven
  NOTHING, talking to one clinic through the link they opened. It reaches the thread routes of
  that clinic and its own four routes, and nothing else — no account exists to reach.
The first two name the login's session row as `sid`, so a logout ends every leg; the third
names a `message_pending_sessions` row instead, which is why no decoder accepts two of them.
A clinic enters an account only by the patient's gesture — the clinic's link on login, or its
link/code pasted later — never by an e-mail match
(docs/CHECKPOINT_portal_clinicas_convite.md, docs/CHECKPOINT_portal_sessao_pendente.md).
"""

import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import ClientDisconnect
from starlette.types import Message, Receive

from brain_api.config import get_settings
from brain_api.core import attachments
from brain_api.core.cookies import (
    clear_patient_pending_cookie,
    clear_patient_session_cookie,
    read_patient_pending_cookie,
    read_patient_session_cookie,
    require_client_header,
    set_patient_pending_cookie,
    set_patient_session_cookie,
)
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.core.ratelimit import SlidingWindowLimiter, client_ip
from brain_api.core.security import (
    create_patient_account_token,
    create_patient_pending_token,
    create_patient_token,
    decode_patient_account_token,
    decode_patient_pending_token,
    decode_patient_token,
)
from brain_api.models import Tenant
from brain_api.models.patient_access import (
    MessagePatient,
    MessagePatientAccount,
    MessagePatientSession,
    MessagePendingSession,
)
from brain_api.schemas.entitlement import EntitlementOut
from brain_api.schemas.patient_access import (
    ClinicInviteIn,
    ClinicLookupIn,
    ClinicPublicOut,
    ClinicSessionOut,
    ConfirmSiblingIn,
    MessageOut,
    OtpRequestIn,
    OtpVerifyIn,
    PatientAccountOut,
    PatientAttachmentForm,
    PatientMessageIn,
    PatientReadMarkIn,
    PendingProgressOut,
    PendingSessionIn,
    PendingSessionOut,
    PendingVerifyIn,
    PublicProductsOut,
    RelayOut,
    SiblingCandidateOut,
    ThreadListOut,
    ThreadOut,
)
from brain_api.services import message_switchboard, patient_access, secretaria_provisioning
from brain_api.services.entitlements import resolve_entitlement

logger = get_logger(__name__)

router = APIRouter(prefix="/patient-access", tags=["patient-access"])

# Three buckets for the unauthenticated login, three abuse cases (config.py says why).
_ip_limiter = SlidingWindowLimiter(
    "patient_otp_ip", lambda: get_settings().PATIENT_OTP_RATE_LIMIT_PER_MIN
)
_email_limiter = SlidingWindowLimiter(
    "patient_otp_email", lambda: get_settings().PATIENT_OTP_EMAIL_RATE_LIMIT_PER_MIN
)
_verify_limiter = SlidingWindowLimiter(
    "patient_otp_verify", lambda: get_settings().PATIENT_VERIFY_RATE_LIMIT_PER_MIN
)
# A fourth, for the authenticated routes that hand out a clinic session without a code (the
# invite and the transition confirm). Keyed by the ACCOUNT, not by IP: behind the portal's
# same-origin proxy the first X-Forwarded-For hop is either client-controlled or the proxy's
# own address, so a per-IP key would be spoofable or one bucket for every patient.
_link_limiter = SlidingWindowLimiter(
    "patient_account_link", lambda: get_settings().PATIENT_LINK_RATE_LIMIT_PER_MIN
)
# A fifth, for the two routes a stranger can reach with NO credential at all (the pending
# visit and the pre-login clinic lookup). Keyed by IP because nothing else exists yet, and
# deliberately the tightest budget in the file: `POST /pending` WRITES an identity and a
# session per call, so this is what stands between a script and a table full of anonymous
# rows. Same caveat as `_ip_limiter` behind the portal proxy, and the same answer: it is the
# only key a request without a session has.
_pending_limiter = SlidingWindowLimiter(
    "patient_pending_open", lambda: get_settings().PATIENT_PENDING_RATE_LIMIT_PER_MIN
)
# A sixth, for uploads (2026-09-18). Keyed by the patient HANDLE for the reason
# `_link_limiter` is keyed by the account; only a message that carries a file spends it, so a
# text conversation is exactly as unthrottled as before.
_attachment_limiter = SlidingWindowLimiter(
    "patient_attachment", lambda: get_settings().PATIENT_ATTACHMENT_RATE_LIMIT_PER_MIN
)
# A seventh, for the uploads of visitors whose e-mail is not verified. Keyed by the CLINIC:
# `POST /pending` mints a fresh identity — and so a fresh `_attachment_limiter` budget — per
# call, so a per-patient key alone lets one script multiply its rate by minting visits. One
# budget shared by all unverified uploads of a clinic bounds the total; a verified patient
# never spends it.
_pending_attachment_limiter = SlidingWindowLimiter(
    "patient_pending_attachment",
    lambda: get_settings().PATIENT_PENDING_ATTACHMENT_RATE_LIMIT_PER_MIN,
)

# The e-mail template secretarIA renders; brain-api owns no SMTP of its own.
_OTP_EMAIL_TEMPLATE = "patient_access_otp"
# The ONE answer `request-otp` ever gives: true whether or not the address, the clinic or
# the channel exist, so the route cannot be used to discover any of them.
_OTP_REQUEST_MESSAGE = "Se esse e-mail puder entrar por aqui, enviamos um código para ele."
# Same message for a wrong, expired, used or never-issued code.
_OTP_INVALID = "Código inválido ou expirado"
_REAUTH_REQUIRED = "reauthentication_required"
_SIBLING_NOT_FOUND = "sibling_not_found"
# One answer for an unparseable invite, an unknown clinic and a clinic with the channel off.
_INVITE_NOT_FOUND = "clinic_invite_not_found"
# The pending visit has no address yet: secretarIA has not claimed one over the internal leg.
# Named, not generic, because this is the one refusal a CLIENT can act on - it means "ask for
# the e-mail in the chat first", not "you are not allowed".
_PENDING_EMAIL_MISSING = "pending_email_missing"


def _bearer_value(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip()


async def _authenticate_patient(
    authorization: str | None, session: AsyncSession
) -> tuple[MessagePatient, MessagePatientSession] | None:
    """The live clinic identity + session row behind a CLINIC token, or `None`.

    All fail-closed and indistinguishable to the caller:

    1. the token decodes with exactly `scope=patient_message` and names `sub`, `tenant_id`,
       `sid` and `login_sid` (an account token, a staff token or a pre-`sid` token fail);
    2. the row `sid` names is live, and so is `login_sid`'s when they differ — so a logout
       ends this leg too (OWASP ASVS 5.0 7.4.1);
    3. the identity exists with the token's tenant, re-read from the row, never trusted from
       the claim;
    4. the identity belongs to the row's account. A row from before the account model that no
       request has attached yet is instead bound to exactly the identity it was issued for.
    """
    token = _bearer_value(authorization)
    if token is None:
        return None
    claims = decode_patient_token(token)
    if claims is None:
        return None
    try:
        patient_ref = UUID(str(claims["sub"]))
        tenant_id = UUID(str(claims["tenant_id"]))
        session_id = UUID(str(claims["sid"]))
        login_session_id = UUID(str(claims["login_sid"]))
    except (ValueError, KeyError):
        return None

    row = await patient_access.find_live_session(session, session_id)
    if row is None:
        return None
    patient = await session.get(MessagePatient, patient_ref)
    if patient is None or patient.tenant_id != tenant_id:
        return None
    if row.account_id is not None:
        if patient.account_id != row.account_id:
            return None
    elif row.patient_id != patient.id or row.tenant_id != tenant_id:
        return None
    if login_session_id != session_id and (
        await patient_access.find_live_session(session, login_session_id) is None
    ):
        return None
    return patient, row


async def _authenticate_account(
    authorization: str | None, session: AsyncSession
) -> tuple[MessagePatientAccount, MessagePatientSession] | None:
    """The account + login row behind an ACCOUNT token, or `None` (a clinic token fails)."""
    token = _bearer_value(authorization)
    if token is None:
        return None
    claims = decode_patient_account_token(token)
    if claims is None:
        return None
    try:
        account_id = UUID(str(claims["sub"]))
        session_id = UUID(str(claims["sid"]))
    except (ValueError, KeyError):
        return None
    row = await patient_access.find_live_session(session, session_id)
    if row is None or row.account_id != account_id:
        return None
    account = await session.get(MessagePatientAccount, account_id)
    if account is None:
        return None
    return account, row


def _expired_patient_cookie_headers() -> dict[str, str]:
    """The `Set-Cookie` that deletes the patient cookie, as raise-able headers.

    FastAPI discards the injected `Response` when a route RAISES, so a rejection that must
    also expire the cookie carries the header on the `HTTPException` itself.
    """
    probe = Response()
    clear_patient_session_cookie(probe)
    return {"set-cookie": probe.headers["set-cookie"]}


async def get_current_patient(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> MessagePatient:
    """Turn a CLINIC token into its live `MessagePatient`, or 401.

    The tenant every thread route acts on is the one returned here — derived from the
    validated token and its rows, never from a header, a query, a cookie or a body.
    """
    if _bearer_value(authorization) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    found = await _authenticate_patient(authorization, session)
    if found is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return found[0]


async def get_current_patient_login(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> tuple[MessagePatient, MessagePatientSession]:
    """`get_current_patient` plus the row its token is bound to (the transition confirm)."""
    if _bearer_value(authorization) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    found = await _authenticate_patient(authorization, session)
    if found is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return found


async def get_current_account(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> tuple[MessagePatientAccount, MessagePatientSession]:
    """Turn an ACCOUNT token into its account + login row, or 401."""
    if _bearer_value(authorization) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    found = await _authenticate_account(authorization, session)
    if found is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return found


# --- The PENDING visit: chat first, e-mail later, code last (2026-09-16) -------------------
#
# A FOURTH surface next to the account's three, and the owner's flow is what forces it:
#
#     link -> chat -> e-mail (typed in the conversation) -> LGPD -> REAL appointment -> code
#
# Every step before the last one happens with nothing proven. The routes below are therefore
# the only ones in this module that a stranger can reach with no credential at all, and each
# one is narrowed by something other than authentication:
#
#   * `POST /clinics/lookup`  reads nothing but three fields the invite link already implies;
#   * `POST /pending`         writes, so it is the tightest per-IP budget in the file;
#   * `POST /pending/request-otp` and `/pending/verify-otp` need a pending token AND an address
#     this repo received from secretarIA, never from the caller.
#
# What a pending token CANNOT do is as important as what it can: it is refused by
# `decode_patient_token` and `decode_patient_account_token`, so it opens no account route, adds
# no clinic, and reaches exactly one tenant — the one whose link minted it.


async def _authenticate_pending(
    authorization: str | None, session: AsyncSession
) -> tuple[MessagePatient, MessagePendingSession] | None:
    """The live identity + visit behind a PENDING token, or `None`. All failures alike.

    Mirrors `_authenticate_patient` step for step, against the OTHER row type:

    1. the token decodes with exactly `scope=patient_pending` and names `sub`, `tenant_id`, `sid`;
    2. the `message_pending_sessions` row `sid` names is live — not expired, not revoked, and
       not already turned into an account login (`_pending_is_live`);
    3. the row's handle and clinic MATCH the claims, re-read from the row rather than trusted;
    4. the identity exists with that clinic.
    """
    token = _bearer_value(authorization)
    if token is None:
        return None
    claims = decode_patient_pending_token(token)
    if claims is None:
        return None
    try:
        patient_ref = UUID(str(claims["sub"]))
        tenant_id = UUID(str(claims["tenant_id"]))
        pending_id = UUID(str(claims["sid"]))
    except (ValueError, KeyError):
        return None

    row = await patient_access.find_live_pending(session, pending_id)
    if row is None or row.patient_id != patient_ref or row.tenant_id != tenant_id:
        return None
    patient = await session.get(MessagePatient, patient_ref)
    if patient is None or patient.tenant_id != tenant_id:
        return None
    return patient, row


async def get_thread_identity(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> tuple[MessagePatient, bool]:
    """The identity behind EITHER a clinic token or a pending token, and which one it was.

    The thread routes take both because a thread is the one thing both populations do: a
    logged-in patient of a clinic, and a visitor still in the conversation that will become
    one. Everything those routes need — `tenant_id` and the handle — comes off the identity
    either way, and neither token can name a clinic it was not minted for.

    The flag is `True` for a clinic token, and only the upload branch reads it. Both kinds may
    send a file — the owner's decision (2026-09-18): any patient, e-mail verified or not — but
    a pending visitor's uploads also share ONE budget per clinic (`_pending_attachment_limiter`),
    because its identity is minted per `POST /pending` call and a per-patient budget alone
    would bound nothing.

    Order matters only for cost: a clinic token is the common case, and a pending token fails
    `decode_patient_token` without a query.
    """
    if _bearer_value(authorization) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    found = await _authenticate_patient(authorization, session)
    if found is not None:
        return found[0], True
    pending = await _authenticate_pending(authorization, session)
    if pending is not None:
        return pending[0], False
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")


async def get_thread_patient(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> MessagePatient:
    """`get_thread_identity` without the flag — what every thread route but the upload needs."""
    patient, _ = await get_thread_identity(authorization, session)
    return patient


async def get_current_pending(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> tuple[MessagePatient, MessagePendingSession]:
    """Turn a PENDING token into its identity + visit, or 401 (a clinic token is refused)."""
    if _bearer_value(authorization) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    found = await _authenticate_pending(authorization, session)
    if found is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return found


def _clinic_session(
    patient: MessagePatient, tenant: Tenant, login_session_id: UUID
) -> ClinicSessionOut:
    """A clinic token bound to the login row (`sid` = `login_sid`): no row per clinic."""
    return ClinicSessionOut(
        access_token=create_patient_token(
            tenant_id=str(patient.tenant_id),
            patient_ref=str(patient.id),
            session_id=str(login_session_id),
            login_session_id=str(login_session_id),
        ),
        expires_in=get_settings().PATIENT_TOKEN_EXPIRE_MINUTES * 60,
        tenant_id=patient.tenant_id,
        clinic_name=tenant.clinic_name,
        patient_ref=patient.id,
    )


async def _account_body(
    session: AsyncSession,
    account: MessagePatientAccount,
    login_session_id: UUID,
    opened_at: MessagePatient | None,
    *,
    invited_tenant_id: UUID | None = None,
    for_refresh: bool = False,
) -> PatientAccountOut:
    """`verify-otp`'s and `refresh`'s body: the account token and one token per clinic.

    The transition fields are filled for the portal deployed on 2026-09-14. Their top-level
    clinic is the one the login came in through — the SAME on every renewal, which that
    portal requires (it drops an account whose login clinic changes) — even if that clinic
    has since switched the channel off, as the pre-account refresh did. A login opened by
    e-mail alone names the clinic it was pinned to (`login_clinic`), and nothing while the
    account has none — never "the first by name", which moves when a clinic is added.
    """
    rows = await patient_access.account_clinics(session, account)
    clinics = [_clinic_session(patient, tenant, login_session_id) for patient, tenant in rows]
    primary: ClinicSessionOut | None = None
    if opened_at is not None:
        primary = next((c for c in clinics if c.tenant_id == opened_at.tenant_id), None)
        if primary is None:
            tenant = await session.get(Tenant, opened_at.tenant_id)
            if tenant is not None:
                primary = _clinic_session(opened_at, tenant, login_session_id)

    body = PatientAccountOut(
        account_token=create_patient_account_token(
            account_id=str(account.id), session_id=str(login_session_id)
        ),
        expires_in=get_settings().PATIENT_TOKEN_EXPIRE_MINUTES * 60,
        clinics=clinics,
        invited_tenant_id=invited_tenant_id,
        sibling_candidates=[
            SiblingCandidateOut(tenant_id=c.tenant_id, clinic_name=c.clinic_name)
            for c in clinics
            if primary is None or c.tenant_id != primary.tenant_id
        ],
        linked_sessions=clinics if for_refresh else [],
    )
    if primary is not None:
        body.access_token = primary.access_token
        body.tenant_id = primary.tenant_id
        body.patient_ref = primary.patient_ref
        body.clinic_name = primary.clinic_name
    return body


@router.post(
    "/request-otp",
    response_model=MessageOut,
    summary="E-mail a login code to a patient",
    description=(
        "Sends a short code to the address. ALWAYS returns the same 200 body. With the "
        "deprecated `tenant_id`, the code is sent only while that clinic has the channel on."
    ),
    responses={429: {"description": "Rate limited (per-IP and per-address budgets)."}},
)
async def request_otp(
    payload: OtpRequestIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    """Begin the login of the ACCOUNT.

    TWO limiters before any DB work: the per-IP bucket protects the service, the
    per-ADDRESS bucket protects the inbox a distributed attacker would otherwise flood. Both
    fail open internally, so neither can 500 the login path.
    """
    address = patient_access.normalize_email(payload.email)
    if not _ip_limiter.allow(client_ip(request)) or not _email_limiter.allow(address):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")

    if payload.tenant_id is not None and (
        await patient_access.channel_open_tenant(session, payload.tenant_id) is None
    ):
        # The pre-account body keeps its old gate: no clinic on the channel, no code.
        logger.info("patient_otp_requested_no_channel")
        return MessageOut(detail=_OTP_REQUEST_MESSAGE)

    code = await patient_access.issue_account_otp(session, address)
    # Fail-soft, like every transactional e-mail in this repo.
    await secretaria_provisioning.send_notification_email(
        address,
        _OTP_EMAIL_TEMPLATE,
        {"code": code, "ttl_minutes": get_settings().PATIENT_OTP_EXPIRE_MINUTES},
    )
    logger.info("patient_otp_requested", with_clinic=payload.tenant_id is not None)
    return MessageOut(detail=_OTP_REQUEST_MESSAGE)


@router.post(
    "/verify-otp",
    response_model=PatientAccountOut,
    summary="Exchange a code for the patient's account session",
    responses={
        400: {"description": "Unknown, wrong, expired or already-used code."},
        422: {"description": "Malformed body, or both `invite` and `tenant_id`."},
        429: {"description": "Rate limited (per-IP verify budget)."},
    },
)
async def verify_otp(
    payload: OtpVerifyIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> PatientAccountOut:
    """Burn the code, open (or create) the account, add the invited clinic, open the session.

    - `invite` (the clinic's link, code or UUID): the first contact through a clinic's link —
      the account comes back with that clinic in it, in one request. An invite that names
      nothing usable does not fail a correct code; `invited_tenant_id` is just null.
    - deprecated `tenant_id`: the portal deployed on 2026-09-14. Login + invite of that
      clinic, and — as before — a clinic without the channel fails with the code's 400,
      checked BEFORE the code is spent.

    Both legs of the session are issued here: the opaque revocable token as the HttpOnly
    cookie, the short tokens in the body for the client to hold IN MEMORY.
    """
    if not _verify_limiter.allow(client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")

    legacy_clinic: Tenant | None = None
    if payload.tenant_id is not None:
        legacy_clinic = await patient_access.channel_open_tenant(session, payload.tenant_id)
        if legacy_clinic is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, _OTP_INVALID)

    if not await patient_access.verify_account_otp(session, payload.email, payload.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _OTP_INVALID)
    account = await patient_access.open_account(session, payload.email)

    clinic = legacy_clinic
    if clinic is None and payload.invite is not None:
        clinic = await patient_access.resolve_invite(session, payload.invite)
    invited = (
        await patient_access.add_clinic(session, account, clinic) if clinic is not None else None
    )
    # The clinic the transition fields name for this login, pinned at insert for its whole
    # life: the one it came in through, else the account's first at this moment.
    opened_at = invited
    if opened_at is None:
        existing = await patient_access.account_clinics(session, account)
        opened_at = existing[0][0] if existing else None

    raw_session, session_id = await patient_access.issue_patient_session(
        session, account, opened_at=opened_at
    )
    set_patient_session_cookie(response, raw_session)
    # The clinic it came in through, if any — never the address, never the account.
    logger.info(
        "patient_session_issued",
        tenant_id=str(invited.tenant_id) if invited is not None else None,
        invite_sent=payload.invite is not None or payload.tenant_id is not None,
    )
    return await _account_body(
        session,
        account,
        session_id,
        opened_at,
        invited_tenant_id=invited.tenant_id if invited is not None else None,
    )


@router.post(
    "/refresh",
    response_model=PatientAccountOut,
    summary="Renew the patient session silently — no code, no prompt",
    description=(
        "Reads the `__Host-patient_session` cookie and answers with a fresh account token "
        "and one token per clinic of the account. The cookie is rotated and its lifetime "
        "slides forward; presenting the value it replaced after a short grace window ends "
        "the whole account (reuse signal)."
    ),
    responses={
        401: {
            "description": (
                "No patient cookie, or one that is unknown, expired, revoked or reused. "
                "A rejected cookie is also expired in the browser."
            )
        },
        403: {"description": "Cookie presented without X-Brain-Client (CSRF guard)."},
    },
)
async def refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> PatientAccountOut:
    """The silent renewal behind "opened the portal, already logged in".

    The cookie is the ONLY credential here, so `require_client_header` runs BEFORE the value
    is spent (auth-jwt-multitenant). What comes back is the account and EVERY clinic in it
    (`clinics`, and `linked_sessions` for the transition portal) — never a clinic the
    account does not have.
    """
    raw = read_patient_session_cookie(request)
    if raw is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing patient session")
    require_client_header(request)

    renewal = await patient_access.rotate_patient_session(session, raw)
    if renewal is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired session",
            headers=_expired_patient_cookie_headers(),
        )
    if renewal.new_raw_token is not None:
        set_patient_session_cookie(response, renewal.new_raw_token)

    opened_at = await patient_access.login_clinic(session, renewal.row, renewal.account)
    body = await _account_body(
        session, renewal.account, renewal.row.id, opened_at, for_refresh=True
    )
    # Counts only — never the address, never a token.
    logger.info(
        "patient_session_refreshed",
        rotated=renewal.new_raw_token is not None,
        clinic_count=len(body.clinics),
    )
    return body


@router.post(
    "/logout",
    response_model=MessageOut,
    summary="End the patient session — the whole account when a bearer is sent",
)
async def logout(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    """Revoke server-side sessions and clear the cookie. Always 200.

    - WITH a valid account token or clinic token: the whole ACCOUNT ends — every live
      session, at every clinic, on every device (`revoke_account_sessions`).
    - WITHOUT one: only the session in this browser's cookie, and with it every token of
      that login (they all name its row).

    Never refused, and no CSRF header: the worst a forged logout achieves is signing someone
    out. ORDER MATTERS: the bearer is resolved BEFORE the cookie's row is revoked — they
    usually name the same row, and revoking first would shrink an account logout into a
    device logout.
    """
    # The address behind whichever valid bearer came — account or clinic token — read off
    # the authenticated row, never from the request. Nothing is created or adopted on the
    # way out.
    address: str | None = None
    account_login = await _authenticate_account(authorization, session)
    if account_login is not None:
        address = account_login[0].email
    else:
        clinic_login = await _authenticate_patient(authorization, session)
        if clinic_login is not None:
            address = clinic_login[0].email

    raw = read_patient_session_cookie(request)
    if raw:
        cookie_row = await patient_access.find_patient_session(session, raw)
        if cookie_row is not None:
            await patient_access.revoke_patient_session(session, cookie_row)

    if address is not None:
        count = await patient_access.revoke_account_sessions(session, address)
        logger.info("patient_account_sessions_revoked", count=count)
    clear_patient_session_cookie(response)
    return MessageOut(detail="Sessão encerrada")


@router.post(
    "/clinics",
    response_model=ClinicSessionOut,
    summary="Add a clinic to the account by its link, short code or id — no code needed",
    description=(
        "The patient opened another clinic's link, or pasted its link or code. Needs the "
        "ACCOUNT token. Idempotent: a clinic already in the account answers with its session "
        "and records nothing new."
    ),
    responses={
        401: {"description": "Missing or invalid account token (a clinic token is refused)."},
        404: {"description": "No usable clinic — one answer for every reason."},
        422: {"description": "Empty, oversized or extra fields."},
        429: {"description": "Rate limited (per-account budget)."},
    },
)
async def add_clinic_by_invite(
    payload: ClinicInviteIn,
    background_tasks: BackgroundTasks,
    login: tuple[MessagePatientAccount, MessagePatientSession] = Depends(get_current_account),
    session: AsyncSession = Depends(get_session),
) -> ClinicSessionOut:
    """Increment the account with the clinic the invite names.

    No code and no recent-login window, by the owner's decision ("apenas incrementar"). The
    risk accepted, stated: whoever holds a live account token (30 minutes, page memory) can
    add clinics to that account and receive their tokens — the same reach page memory
    already has, since every clinic token of the account lives there too, and nothing
    outside that account becomes readable. The inbox was proven by the login's code.

    THE SECOND GREETING TRIGGER (TASK-003 §2, correction of 2026-09-19). The owner asked for
    the automation to speak first to a patient who is new **to the clinic**, not new to the
    Portal — and that patient arrives HERE, not at `/pending`: they already have an account,
    and opening a new clinic's link adds it to that account. Gating this on "was the clinic
    actually new" is deliberately NOT done: the honest answer is not "is the row new" (the
    identity may predate this account) but "does a conversation with a message exist", which
    only secretarIA knows. It owns that answer and returns `exists` without sending anything,
    so the greeting stays single without a second, drifting copy of the rule living here.
    """
    account, login_row = login
    login_session_id = login_row.id
    if not _link_limiter.allow(str(account.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    clinic = await patient_access.resolve_invite(session, payload.invite)
    if clinic is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _INVITE_NOT_FOUND)
    patient = await patient_access.add_clinic(session, account, clinic)
    if patient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _INVITE_NOT_FOUND)
    if login_row.patient_id is None:
        # A login opened by e-mail alone names its first clinic from now on (`login_clinic`).
        await patient_access.pin_login_clinic(session, login_session_id, patient)
    # After `add_clinic`, which commits. No `product` to honour: this route carries no link
    # parameter, so the portal lands on whatever tab `default_product` names.
    _greet_if_secretaria(
        background_tasks,
        await resolve_entitlement(session, clinic.id),
        tenant_id=clinic.id,
        patient_ref=patient.id,
    )
    return _clinic_session(patient, clinic, login_session_id)


@router.post(
    "/siblings/{tenant_id}/confirm",
    response_model=ClinicSessionOut,
    deprecated=True,
    summary="TRANSITION ONLY — reopen a clinic that is already in the account",
    description=(
        "For the portal deployed on 2026-09-14, which reopens each `sibling_candidates` entry "
        "marked `already_linked`. Links nothing: a clinic outside the account is a 404. Needs "
        "the login's clinic token AND that login's cookie. Remove with that portal."
    ),
    responses={
        401: {"description": "Invalid token, or a token not bound to this browser's login."},
        404: {"description": "Not a clinic of this account — one answer for every reason."},
        422: {"description": "The body names a different clinic than the path."},
        429: {"description": "Rate limited (per-account budget)."},
    },
)
async def confirm_sibling(
    payload: ConfirmSiblingIn,
    request: Request,
    tenant_id: UUID,
    login: tuple[MessagePatient, MessagePatientSession] = Depends(get_current_patient_login),
    session: AsyncSession = Depends(get_session),
) -> ClinicSessionOut:
    """Hand the transition portal a token for a clinic the account ALREADY has.

    Kept gated as before — the bearer's row must be this browser's cookie row — because a
    clinic token must not become every clinic's token from another device. Nothing new is
    granted, so there is no recent-login window any more.
    """
    if payload.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "tenant_mismatch")
    _, login_row = login
    raw = read_patient_session_cookie(request)
    cookie_row = await patient_access.find_patient_session(session, raw) if raw else None
    if cookie_row is None or cookie_row.id != login_row.id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _REAUTH_REQUIRED)
    account = await patient_access.session_account(session, cookie_row)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _SIBLING_NOT_FOUND)
    if not _link_limiter.allow(str(account.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    found = await patient_access.account_clinics(session, account, tenant_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _SIBLING_NOT_FOUND)
    patient, clinic = found[0]
    return _clinic_session(patient, clinic, login_row.id)


@router.get(
    "/threads",
    response_model=ThreadListOut,
    summary="Which product threads this patient may open",
    responses={401: {"description": "Missing or invalid patient session."}},
)
async def list_threads(
    patient: MessagePatient = Depends(get_thread_patient),
    session: AsyncSession = Depends(get_session),
) -> ThreadListOut:
    """Exactly `EntitlementOut.products` intersected with `channels.brain_message`.

    An EMPTY list, not a 403, when the channel is off: the patient holds a valid session and
    the honest answer is "there is nothing here". The 403 lives on the per-thread routes.
    """
    ent = await resolve_entitlement(session, patient.tenant_id)
    products = message_switchboard.available_products(ent)
    logger.info(
        "patient_threads_listed",
        tenant_id=str(patient.tenant_id),
        count=len(products),
    )
    return ThreadListOut(data=[ThreadOut(product=p, clinic_name=ent.clinic_name) for p in products])


# --- Sending: JSON (text) or multipart (a file), one resource (2026-09-18) ------------------

# Ceiling for a body with no file: the JSON variant, and each text field of the multipart one.
# Generous — the real bounds are the models' (4000 + 200 + 256 characters) — it only keeps a
# body from being read into memory unbounded now that this route reads it itself.
_TEXT_BODY_LIMIT = 64 * 1024
# `PatientAttachmentForm` has three fields; the slack lets an extra one be refused BY NAME
# (the model's 422) instead of by count.
_MAX_FORM_FIELDS = 8
# FastAPI's own words for a body it could not read at all; kept so the answer is unchanged.
_BODY_UNREADABLE = "There was an error parsing the body"

_ATTACHMENT_FORM_SCHEMA = {
    "type": "object",
    "required": ["file"],
    "properties": {
        "file": {
            "type": "string",
            "format": "binary",
            "description": (
                "ONE file: JPEG, PNG, WEBP, GIF or PDF, judged by its content, up to "
                f"{attachments.MAX_ATTACHMENT_BYTES} bytes."
            ),
        },
        **PatientAttachmentForm.model_json_schema()["properties"],
    },
    "additionalProperties": False,
}


class _BodyTooLarge(MultiPartException):
    """Raised from inside the body stream when a request outgrows its cap.

    A `MultiPartException` on purpose: Starlette's parser closes — and so deletes — every
    part it spooled when one escapes it, so an aborted upload leaves nothing behind.
    """


class _CappedReceive:
    """An ASGI `receive` that refuses to deliver more than `limit` body bytes.

    Content-Length is checked before a byte is read, but a chunked body has none: this is
    what bounds that case, WHILE it streams and before the parser spools any of it.
    """

    def __init__(self, receive: Receive, limit: int) -> None:
        self._receive = receive
        self._limit = limit
        self._seen = 0
        self.exceeded = False

    async def __call__(self) -> Message:
        message = await self._receive()
        if message["type"] == "http.request":
            self._seen += len(message.get("body", b""))
            if self._seen > self._limit:
                self.exceeded = True
                raise _BodyTooLarge("request body over the cap")
        return message


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    return int(raw) if raw is not None and raw.isdigit() else None


def _body_errors(exc: ValidationError) -> RequestValidationError:
    """Pydantic's errors placed under `body`, as FastAPI reports those of a declared body."""
    return RequestValidationError(
        [{**error, "loc": ("body", *error["loc"])} for error in exc.errors(include_url=False)]
    )


async def _read_json_message(request: Request) -> PatientMessageIn:
    """The JSON body, judged exactly as FastAPI judged it while the route still declared it.

    FastAPI's strict content type, rule for rule: an empty body is a missing one, a body that
    is not `application/json` (or `+json`) is not an object, bad JSON is `json_invalid`, and
    every model error sits under `body` — so the JSON client sees the same 422 it always did.
    """
    declared = _declared_length(request)
    receive = _CappedReceive(request.receive, _TEXT_BODY_LIMIT)
    try:
        if declared is not None and declared > _TEXT_BODY_LIMIT:
            raise _BodyTooLarge("declared body over the cap")
        raw = await Request(request.scope, receive).body()
    except _BodyTooLarge:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "body_too_large") from None
    except ClientDisconnect:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _BODY_UNREADABLE) from None
    if not raw:
        raise _missing_body()
    media = _media_type(request)
    is_json = media == "application/json" or (
        media.startswith("application/") and media.endswith("+json")
    )
    if not is_json:
        raise _not_an_object(raw.decode("utf-8", errors="replace"))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RequestValidationError(
            [
                {
                    "type": "json_invalid",
                    "loc": ("body", exc.pos),
                    "msg": "JSON decode error",
                    "input": {},
                    "ctx": {"error": exc.msg},
                }
            ]
        ) from None
    except (ValueError, RecursionError):
        # Bytes that are no Unicode, a number past Python's digit limit, nesting past the
        # recursion limit: FastAPI answers each with this 400 — never a 500 and a traceback.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _BODY_UNREADABLE) from None
    if data is None:  # JSON `null` is an absent body to FastAPI, and so here
        raise _missing_body()
    if not isinstance(data, dict):  # an array, a string, a number: not an object
        raise _not_an_object(data)
    try:
        return PatientMessageIn.model_validate(data)
    except ValidationError as exc:
        raise _body_errors(exc) from None


def _missing_body() -> RequestValidationError:
    return RequestValidationError(
        [{"type": "missing", "loc": ("body",), "msg": "Field required", "input": None}]
    )


def _not_an_object(value: object) -> RequestValidationError:
    return RequestValidationError(
        [
            {
                "type": "model_attributes_type",
                "loc": ("body",),
                "msg": "Input should be a valid dictionary or object to extract fields from",
                "input": value,
            }
        ]
    )


def _attachment_parts(form: FormData) -> tuple[PatientAttachmentForm, UploadFile]:
    """The ONE file part (`file`) and the text fields, the latter judged like the JSON body."""
    items = form.multi_items()
    uploads = [(key, value) for key, value in items if isinstance(value, UploadFile)]
    if len(uploads) != 1 or uploads[0][0] != "file":
        raise attachments.AttachmentRefused(attachments.ATTACHMENT_MALFORMED)
    texts = [(key, value) for key, value in items if isinstance(value, str)]
    if len({key for key, _ in texts}) != len(texts):  # the same field twice
        raise attachments.AttachmentRefused(attachments.ATTACHMENT_MALFORMED)
    try:
        fields = PatientAttachmentForm.model_validate({k: v for k, v in texts if v != ""})
    except ValidationError as exc:
        raise _body_errors(exc) from None
    return fields, uploads[0][1]


async def _relay_attachment(
    request: Request, product: str, patient: MessagePatient, verified: bool
) -> RelayOut:
    """The multipart branch. Every refusal is logged by its CODE, with the clinic and the
    product — never the file's name (patients name files after themselves), never a byte."""
    try:
        return await _checked_relay(request, product, patient, verified)
    except attachments.AttachmentRefused as refused:
        logger.info(
            "patient_attachment_refused",
            tenant_id=str(patient.tenant_id),
            product=product,
            code=refused.code,
        )
        raise HTTPException(refused.status_code, refused.detail) from None


async def _checked_relay(
    request: Request, product: str, patient: MessagePatient, verified: bool
) -> RelayOut:
    """Every cheap refusal first, then the body, then the file itself.

    Cost order: nothing reads a byte of the file until the product takes files and the patient
    still has budget — its own, and for a visitor whose e-mail is not verified, its clinic's
    shared one too (`get_thread_identity` says why).
    The size is refused as declared before the body is read, and as streamed while it is
    read. Only then is the file sniffed, checked and relayed — and every part the parser
    finished is closed, which deletes its spool (memory up to 1 MiB, then an anonymous
    temporary file), in `finally` whatever happened. A part it never finished — a truncated
    or broken body — is Starlette's to drop and goes with the garbage collector. Nothing is
    ever kept.
    """
    if (
        product not in message_switchboard.ATTACHMENT_PRODUCTS
        or not get_settings().PATIENT_ATTACHMENTS_ENABLED
    ):
        raise attachments.AttachmentRefused(attachments.ATTACHMENT_UNSUPPORTED_FOR_PRODUCT)
    if not _attachment_limiter.allow(str(patient.id)) or (
        not verified and not _pending_attachment_limiter.allow(str(patient.tenant_id))
    ):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    cap = attachments.MAX_ATTACHMENT_BYTES + attachments.MULTIPART_OVERHEAD_BYTES
    declared = _declared_length(request)
    if declared is not None and declared > cap:
        raise attachments.AttachmentRefused(attachments.ATTACHMENT_TOO_LARGE)

    receive = _CappedReceive(request.receive, cap)
    try:
        form = await Request(request.scope, receive).form(
            max_files=1, max_fields=_MAX_FORM_FIELDS, max_part_size=_TEXT_BODY_LIMIT
        )
    except StarletteHTTPException:
        # Starlette answers every parser refusal with a 400 that names its own limits; the
        # patient gets ours instead — 413 for the size cap, one "malformed" for the rest.
        code = (
            attachments.ATTACHMENT_TOO_LARGE
            if receive.exceeded
            else attachments.ATTACHMENT_MALFORMED
        )
        raise attachments.AttachmentRefused(code) from None
    except (ValueError, ClientDisconnect):
        # What Starlette does not translate: python-multipart's own parse errors (a broken
        # part header) and a client that went away mid-body. Malformed, not a 500.
        raise attachments.AttachmentRefused(attachments.ATTACHMENT_MALFORMED) from None
    try:
        fields, upload = _attachment_parts(form)
        head = await upload.read(attachments.SNIFF_BYTES)
        await upload.seek(0)
        checked = attachments.check_attachment(head, upload.size or 0, upload.filename)
        result = await message_switchboard.send_attachment(
            product,
            tenant_id=patient.tenant_id,
            patient_ref=str(patient.id),
            attachment=checked,
            file=upload.file,
            text=fields.text,
            patient_name=fields.patient_name,
            interactive_reply_id=fields.interactive_reply_id,
        )
    finally:
        await form.close()
    logger.info(
        "patient_attachment_relayed",
        tenant_id=str(patient.tenant_id),
        patient_ref=str(patient.id),
        product=product,
        verified=verified,
        content_type=checked.kind.content_type,
        size_bytes=checked.size_bytes,
    )
    return RelayOut(product=product, payload=result, at=datetime.now(UTC))


@router.post(
    "/threads/{product}/messages",
    response_model=RelayOut,
    summary="Send one message on a product thread — text, or a file with an optional caption",
    description=(
        "`application/json` (`PatientMessageIn`) sends text, exactly as before. "
        "`multipart/form-data` sends ONE file in the part `file` plus the same fields as form "
        "fields, `text` becoming an optional caption: JPEG, PNG, WEBP, GIF or PDF judged by "
        "its content, up to 20 MiB, only on a product that takes files (secretaria) — from any "
        "patient, clinic or pending token alike. Refusals of a file are 4xx with "
        '`{"detail": {"code", "message"}}` (docs/CHECKPOINT_brain_message_anexos.md).'
    ),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": PatientMessageIn.model_json_schema()},
                "multipart/form-data": {"schema": _ATTACHMENT_FORM_SCHEMA},
            },
        }
    },
    responses={
        401: {"description": "Missing or invalid patient session."},
        403: {"description": "The clinic does not offer that product on this channel."},
        413: {"description": "The file, or the whole body, is over the ceiling."},
        415: {"description": "The file's real type is not an accepted one."},
        422: {
            "description": (
                "Malformed body; a file whose name contradicts its content; or a product that "
                "takes no files (precheck)."
            )
        },
        429: {
            "description": (
                "Rate limited: the per-patient upload budget, and for a pending (unverified) "
                "visitor also the clinic's shared one."
            )
        },
        502: {"description": "The product backend failed or is misconfigured."},
        503: {"description": "That product's leg of the mesh is unconfigured or degraded."},
    },
)
async def send_thread_message(
    request: Request,
    product: str = Path(
        description="secretaria | precheck — chosen by the CLIENT, never inferred."
    ),
    identity: tuple[MessagePatient, bool] = Depends(get_thread_identity),
    session: AsyncSession = Depends(get_session),
) -> RelayOut:
    """Relay to the named product's internal inbound endpoint.

    `tenant_id` and the patient handle come from the SESSION, never from the body or the
    path: there is no input a patient could point at another clinic. `require_product` runs
    BEFORE any upstream call, so an unowned product costs zero network and leaks nothing.

    TWO ENCODINGS OF ONE RESOURCE (2026-09-18). JSON is the original contract, unchanged.
    Multipart is a message WITH a file, relayed as multipart end to end — never base64, which
    would inflate it by a third and force the whole file into memory to re-encode.

    The body is read HERE rather than declared, and that is a security property: FastAPI
    parses a declared body before any dependency runs, so a declared `UploadFile` would let an
    UNAUTHENTICATED caller make this service spool an unbounded upload. Here the session and
    the entitlement are settled before the first byte of the body is read.
    """
    patient, verified = identity
    ent = await resolve_entitlement(session, patient.tenant_id)
    message_switchboard.require_product(ent, product)
    # The database is done with — release its connection BEFORE any client-paced I/O. The
    # session's own teardown runs only after the response is sent, so otherwise a patient
    # dribbling a body (or a slow upload on a mobile link) holds a pooled connection all the
    # while, and ~15 of them starve every other route of this service. Nothing here commits:
    # closing ends a read-only transaction, and `patient`'s loaded fields stay readable.
    await session.close()
    if _media_type(request) == "multipart/form-data":
        return await _relay_attachment(request, product, patient, verified)

    payload = await _read_json_message(request)
    result = await message_switchboard.send_message(
        product,
        tenant_id=patient.tenant_id,
        patient_ref=str(patient.id),
        text=payload.text,
        patient_name=payload.patient_name,
        interactive_reply_id=payload.interactive_reply_id,
    )
    logger.info(
        "patient_message_relayed",
        tenant_id=str(patient.tenant_id),
        patient_ref=str(patient.id),
        product=product,
    )
    return RelayOut(product=product, payload=result, at=datetime.now(UTC))


@router.get(
    "/threads/{product}/messages",
    response_model=RelayOut,
    summary="Poll one product thread for new messages",
    responses={
        401: {"description": "Missing or invalid patient session."},
        403: {"description": "The clinic does not offer that product on this channel."},
        502: {"description": "The product backend failed or is misconfigured."},
        503: {"description": "That product's leg of the mesh is unconfigured or degraded."},
    },
)
async def poll_thread_messages(
    product: str = Path(description="secretaria | precheck."),
    since: str | None = Query(
        default=None,
        description=(
            "Cursor, passed to the product verbatim. On secretaria it means rows CHANGED "
            "strictly after this instant — new ones and ones whose delivery state moved — so "
            "the same message may come back on a later poll: upsert by `id`, never append, and "
            "take the next cursor from the largest `updated_at` received."
        ),
    ),
    patient: MessagePatient = Depends(get_thread_patient),
    session: AsyncSession = Depends(get_session),
) -> RelayOut:
    """Poll the product's transcript for this patient.

    `since` is passed through opaque: each backend defines its own cursor, and re-formatting
    a value this service does not own is how a cursor silently starts skipping a row. Each
    message's delivery state (`status`, `delivered_at`, `read_at`, `updated_at` on secretaria)
    is the product's and passes through as it came (`message_switchboard.list_messages`).
    """
    ent = await resolve_entitlement(session, patient.tenant_id)
    message_switchboard.require_product(ent, product)
    # Release the pooled connection before the upstream hop, same as `send_thread_message`
    # and `mark_thread_read` — this is the MOST called route (every client poll), so holding
    # a connection here for the duration of the upstream request is the fastest way to starve
    # the pool.
    await session.close()

    result = await message_switchboard.list_messages(
        product,
        tenant_id=patient.tenant_id,
        patient_ref=str(patient.id),
        since=since,
    )
    return RelayOut(product=product, payload=result, at=datetime.now(UTC))


@router.post(
    "/threads/{product}/messages/read",
    response_model=RelayOut,
    summary="Mark the clinic's messages on a product thread as read by the patient",
    description=(
        "Called when the portal SHOWS the thread to the patient. Body: exactly one of "
        "`up_to_message_id` (the last message seen) or `up_to` (an offset-aware instant). The "
        "clinic's messages up to that cursor become `lido`; idempotent. The conversation is the "
        "session's own — the body cannot name a clinic or a patient. The payload is the "
        'product\'s `{"marked", "applied"}`; a product without read receipts (precheck) answers '
        '`{"marked": 0, "applied": false}` and changes nothing.'
    ),
    responses={
        401: {"description": "Missing or invalid patient session."},
        403: {"description": "The clinic does not offer that product on this channel."},
        422: {"description": "Not exactly one cursor, a naive `up_to`, or any other field."},
        502: {"description": "The product backend failed or is misconfigured."},
        503: {"description": "That product's leg of the mesh is unconfigured or degraded."},
    },
)
async def mark_thread_read(
    payload: PatientReadMarkIn,
    product: str = Path(description="secretaria | precheck."),
    patient: MessagePatient = Depends(get_thread_patient),
    session: AsyncSession = Depends(get_session),
) -> RelayOut:
    """Relay the patient's read mark, scoped by the SESSION like every thread route.

    `tenant_id` and `external_id` upstream are `patient.tenant_id` and `patient.id` — never
    the body, which `PatientReadMarkIn` keeps closed — so a patient can mark as read only the
    conversation `GET .../messages` would show them. `require_product` first, as everywhere:
    an unowned product costs no network. No limiter of its own, on purpose: it is held to the
    same budget as sending a text message, which has none either (decision 4 of the prompt);
    the upstream write is one idempotent UPDATE, cheaper than the agent turn a send enqueues.
    """
    ent = await resolve_entitlement(session, patient.tenant_id)
    message_switchboard.require_product(ent, product)
    # Release the pooled connection before the upstream hop (up to its timeout) — the portal
    # calls this on every poll that brings something new, see `send_thread_message`.
    await session.close()

    result = await message_switchboard.mark_read(
        product,
        tenant_id=patient.tenant_id,
        patient_ref=str(patient.id),
        up_to_message_id=payload.up_to_message_id,
        up_to=payload.up_to,
    )
    logger.info(
        "patient_thread_read_marked",
        tenant_id=str(patient.tenant_id),
        product=product,
        applied=result.get("applied"),
        marked=result.get("marked"),
    )
    return RelayOut(product=product, payload=result, at=datetime.now(UTC))


def _media_headers(media: message_switchboard.MediaStream) -> dict[str, str]:
    """What makes a stored file safe to hand a browser from this origin.

    `nosniff` pins the type the switchboard verified; the CSP sandbox disarms anything active
    should the URL ever be opened as a page; `no-store` keeps a patient's file out of every
    cache on the way and off the browser's disk; CORP stops other sites embedding it. Images
    go `inline`, a PDF as a download. The name is generic on purpose: the real one — which
    may carry PII — is the transcript's `attachment.filename`, and a header is one more
    place it would be copied into logs.
    """
    disposition = "inline" if media.kind.family == "image" else "attachment"
    headers = {
        "Content-Disposition": f'{disposition}; filename="anexo.{media.kind.extension}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "private, no-store",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    if media.content_length is not None:
        headers["Content-Length"] = str(media.content_length)
    return headers


@router.get(
    "/threads/{product}/media/{message_id}",
    response_class=StreamingResponse,
    summary="Download one file of this patient's own thread",
    description=(
        "Streams the file a transcript message carries (its `attachment.media_path`). Needs "
        "the same bearer as the thread (clinic or pending token), so the browser fetches the "
        "bytes and renders them itself — the URL alone opens nothing. 404 alike for a file "
        "that does not exist and for one of another conversation."
    ),
    responses={
        200: {
            "description": "The file's bytes, typed as verified.",
            "content": {content_type: {} for content_type in attachments.ALLOWED_CONTENT_TYPES},
        },
        401: {"description": "Missing or invalid patient session."},
        403: {"description": "The clinic does not offer that product on this channel."},
        404: {"description": "No such file IN THIS CONVERSATION — one answer for every reason."},
        422: {"description": "A malformed id."},
        502: {"description": "The product backend failed, or answered a type it may not."},
        503: {"description": "That product's leg of the mesh is unconfigured or degraded."},
    },
)
async def get_thread_media(
    product: str = Path(description="secretaria | precheck."),
    message_id: str = Path(
        pattern=attachments.MEDIA_ID_PATTERN,
        description="The `id` of the message that carries the file, as the poll returned it.",
    ),
    patient: MessagePatient = Depends(get_thread_patient),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Stream one attachment back to the patient whose conversation holds it.

    OWNERSHIP IS NEVER DECIDED BY THE ID. The id names a file; whose file it may be is the
    session's clinic and handle, which travel with every upstream request, and the product
    answers 404 unless the message belongs to THAT conversation. Another patient asking for
    the same id gets the very same 404 as an id that never existed — this route never
    confirms a file exists to someone who cannot read it.

    A stream through here, never a signed bucket URL: the portal's CSP is
    `img-src 'self' data:`, so a third-party storage URL would be blocked, and a URL that
    works for whoever holds it is exactly the kind of credential this module avoids. The bytes
    pass through without being written anywhere.
    """
    ent = await resolve_entitlement(session, patient.tenant_id)
    message_switchboard.require_product(ent, product)
    await session.close()  # before the client-paced download — see `send_thread_message`
    media = await message_switchboard.open_media(
        product, tenant_id=patient.tenant_id, patient_ref=str(patient.id), message_id=message_id
    )
    logger.info(
        "patient_media_streamed",
        tenant_id=str(patient.tenant_id),
        product=product,
        content_type=media.kind.content_type,
    )
    return StreamingResponse(
        media.chunks,
        media_type=media.kind.content_type,
        headers=_media_headers(media),
        background=BackgroundTask(media.aclose),
    )


def _greet_if_secretaria(
    background_tasks: BackgroundTasks,
    ent: EntitlementOut,
    *,
    tenant_id: UUID,
    patient_ref: UUID,
    product: str | None = None,
) -> None:
    """Schedule the clinic's automation to SPEAK FIRST, once this response has been written.

    The owner's requirement (TASK-003 §2): "todo link aberto por um paciente novo da clínica
    (...) é preciso que a nossa automação mande mensagem automaticamente, sem a necessidade do
    paciente mandar a mensagem". Until now a patient who opened a link landed in an EMPTY
    conversation, because secretarIA's conversation is born from an inbound message and there
    was none.

    WHY A BACKGROUND TASK AND NOT AN `await` HERE. Starlette runs it after the response body
    is sent, so the two properties the contract demands are structural rather than hopeful: a
    failure cannot turn this route's `200` into an error (the answer is already gone), and a
    slow or hung secretarIA cannot add a single millisecond the patient can perceive. A short
    timeout would have bounded the damage; it would not have removed it.

    ONLY FOR secretarIA, AND THAT IS A LIMITATION, NOT AN OVERSIGHT. `product="precheck"`
    triggers nothing at all. PreCheck's session is opened either by its `/internal/precheck-
    handoff` (which is keyed on a WhatsApp phone number a Portal patient does not have) or by
    a patient message — and relaying a message the patient never sent is exactly the
    fabricated bubble TASK-003 forbids. Giving PreCheck the same treatment needs a route in
    the PreCheck repo, which this task may not touch. A PreCheck thread therefore stays empty
    until the patient writes, as it does today. See `results/brain-api.md` §"peça 3".

    `product` is the one the LINK named; `None` means the portal will land on
    `default_product(ent)` — the same first-offered tab it renders.
    """
    resolved = product or message_switchboard.default_product(ent)
    if resolved != message_switchboard.PRODUCT_SECRETARIA:
        return
    background_tasks.add_task(
        message_switchboard.open_conversation,
        tenant_id=tenant_id,
        patient_ref=str(patient_ref),
    )


async def _public_products(session: AsyncSession, tenant_id: UUID) -> PublicProductsOut:
    """The clinic's products AS REACHABLE ON THIS CHANNEL — the same computation as `/threads`.

    `available_products` is deliberately reused rather than reading `ent.products` directly:
    it applies both gates (the product AND `channels.brain_message`), so what a visitor is
    offered before logging in cannot disagree with the tabs they get after.
    """
    ent = await resolve_entitlement(session, tenant_id)
    offered = set(message_switchboard.available_products(ent))
    return PublicProductsOut(
        secretaria=message_switchboard.PRODUCT_SECRETARIA in offered,
        precheck=message_switchboard.PRODUCT_PRECHECK in offered,
    )


@router.post(
    "/clinics/lookup",
    response_model=ClinicPublicOut,
    summary="What a clinic offers on this channel — before any login",
    description=(
        "Resolves the clinic's link, short code or id and answers with its name and the "
        "products reachable on the Brain-Message channel. No session, no e-mail, no code: it "
        "is what the portal needs to render the secretarIA/PreCheck toggle on a first visit."
    ),
    responses={
        404: {"description": "No usable clinic — one answer for every reason."},
        429: {"description": "Rate limited (per-IP budget)."},
    },
)
async def lookup_clinic(
    payload: ClinicLookupIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ClinicPublicOut:
    """The pre-login half of `GET /entitlements`, narrowed to what a visitor may know.

    Three fields leave: the clinic's id, its name, and two booleans. Plan, status, limits,
    usage and add-ons stay behind the staff token — a visitor has no business reading what a
    clinic pays, and `false` for "never bought" is indistinguishable here from `false` for
    "subscription lapsed", which is the honest answer either way.
    """
    if not _pending_limiter.allow(client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    clinic = await patient_access.resolve_invite(session, payload.invite)
    if clinic is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _INVITE_NOT_FOUND)
    products = await _public_products(session, clinic.id)
    logger.info("clinic_public_lookup", tenant_id=str(clinic.id))
    return ClinicPublicOut(tenant_id=clinic.id, clinic_name=clinic.clinic_name, products=products)


@router.post(
    "/pending",
    response_model=PendingSessionOut,
    summary="Open (or resume) a conversation with no login at all",
    description=(
        "Mints the handle secretarIA and PreCheck will know this visitor by, plus a token "
        "scoped to that one clinic and one HttpOnly cookie so a reload resumes the same "
        "conversation. Presenting a live pending cookie for the SAME clinic resumes it "
        "instead of minting a second one."
    ),
    responses={
        403: {"description": "The clinic does not offer that product on this channel."},
        404: {"description": "No usable clinic — one answer for every reason."},
        429: {"description": "Rate limited (per-IP budget)."},
    },
)
async def open_pending(
    payload: PendingSessionIn,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> PendingSessionOut:
    """The door a patient with no account walks through — for EITHER product.

    `product` is checked with `require_product`, the same function the relay uses, so a direct
    PreCheck link to a clinic without PreCheck is refused here rather than at the first
    message. There is deliberately NO booking gate: the owner closed that on 2026-09-16
    ("todos os produtos, sem gate"). Opening PreCheck automatically right after secretarIA
    confirms an appointment stays a proactive behaviour elsewhere — it is not a condition of
    access, and nothing in this file treats it as one.

    NOTHING IS PRE-CREATED UPSTREAM — **except secretarIA's greeting, since 2026-09-19**.
    PreCheck's conductor opens its own session on the first inbound message (`resolve_session`
    is idempotent by `session_ref`), so the only way to pre-create one here would be to relay a
    synthetic message the patient never sent; for PreCheck that is still true and still
    refused. secretarIA now has a route that opens the conversation and greets WITHOUT an
    inbound (`_greet_if_secretaria`), so a patient who opens a link is spoken to first instead
    of landing in an empty thread. It fires only on a CREATED visit — a reload that resumes
    this same visit through the cookie must not produce a second greeting — and only when the
    resolved product is secretarIA.

    RESUME, AND ITS ONE LIMIT: the cookie holds the most recent visit. Opening a DIFFERENT
    clinic's link replaces it, and the previous conversation stops being reachable from this
    browser (the row itself lives out its expiry). Accepted for this round — holding several
    clinics at once is what the ACCOUNT does, and it needs a proven address to exist.
    """
    if not _pending_limiter.allow(client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    clinic = await patient_access.resolve_invite(session, payload.invite)
    if clinic is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _INVITE_NOT_FOUND)

    ent = await resolve_entitlement(session, clinic.id)
    if payload.product is not None:
        message_switchboard.require_product(ent, payload.product)
    offered = set(message_switchboard.available_products(ent))
    products = PublicProductsOut(
        secretaria=message_switchboard.PRODUCT_SECRETARIA in offered,
        precheck=message_switchboard.PRODUCT_PRECHECK in offered,
    )

    raw_cookie = read_patient_pending_cookie(request)
    pending = (
        await patient_access.find_pending_by_token(session, raw_cookie) if raw_cookie else None
    )
    patient: MessagePatient | None = None
    if pending is not None and pending.tenant_id == clinic.id:
        patient = await session.get(MessagePatient, pending.patient_id)
    resumed = patient is not None
    if not resumed:
        pending, patient, raw_cookie = await patient_access.open_pending_session(session, clinic)
        set_patient_pending_cookie(response, raw_cookie)
        # AFTER the visit is committed (`open_pending_session` commits), so secretarIA can
        # already read the handle back over its own service leg when it runs.
        _greet_if_secretaria(
            background_tasks,
            ent,
            tenant_id=clinic.id,
            patient_ref=patient.id,
            product=payload.product,
        )

    # The clinic and whether this was a resume — never the handle, never the address.
    logger.info("patient_pending_opened", tenant_id=str(clinic.id), resumed=resumed)
    return PendingSessionOut(
        pending_token=create_patient_pending_token(
            tenant_id=str(clinic.id), patient_ref=str(patient.id), session_id=str(pending.id)
        ),
        expires_in=get_settings().PATIENT_TOKEN_EXPIRE_MINUTES * 60,
        tenant_id=clinic.id,
        clinic_name=clinic.clinic_name,
        patient_ref=patient.id,
        products=products,
        email_claimed=pending.email is not None,
    )


@router.get(
    "/pending/status",
    response_model=PendingProgressOut,
    summary="Read the pending chat's identity step without exposing identity data",
    responses={401: {"description": "Missing or invalid pending token."}},
)
async def pending_status(
    visit: tuple[MessagePatient, MessagePendingSession] = Depends(get_current_pending),
) -> PendingProgressOut:
    """The typed state the Portal uses for its composer; no copy matching required."""
    _, pending = visit
    if pending.verified_at is not None:
        state = "verified"
    elif patient_access.pending_otp_is_active(pending):
        state = "otp_sent"
    elif pending.email is not None:
        state = "pending_claimed"
    else:
        state = "pending_unclaimed"
    return PendingProgressOut(state=state)


@router.post(
    "/pending/complete",
    response_model=PatientAccountOut,
    summary="Exchange an inline-verified visit for the browser's account session",
    responses={
        401: {"description": "Missing or invalid pending token."},
        409: {"description": "The inline code has not been verified yet."},
    },
)
async def complete_pending(
    response: Response,
    visit: tuple[MessagePatient, MessagePendingSession] = Depends(get_current_pending),
    session: AsyncSession = Depends(get_session),
) -> PatientAccountOut:
    """The only leg allowed to promote the browser after secretarIA verified its code.

    The service callback cannot set a cookie in the patient's browser and must never send
    tokens through the transcript. This request already carries the scoped pending bearer;
    it atomically spends that visit, writes the HttpOnly account cookie and returns the same
    `PatientAccountOut` every ordinary OTP login uses.
    """
    _, pending = visit
    completed = await patient_access.complete_pending_identity(session, pending)
    if completed is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "pending_verification_incomplete")
    account, canonical, raw_session, session_id = completed
    set_patient_session_cookie(response, raw_session)
    clear_patient_pending_cookie(response)
    return await _account_body(
        session,
        account,
        session_id,
        canonical,
        invited_tenant_id=canonical.tenant_id,
    )


@router.post(
    "/pending/request-otp",
    response_model=MessageOut,
    summary="E-mail the code to the address THIS conversation captured",
    description=(
        "Takes no address: the one used is `message_pending_sessions.email`, written by "
        "secretarIA over the internal leg when the patient typed it in the chat. Always the "
        "same 200 body once the visit has one."
    ),
    responses={
        401: {"description": "Missing or invalid pending token."},
        409: {"description": "This conversation has not captured an e-mail yet."},
        429: {"description": "Rate limited (per-IP and per-address budgets)."},
    },
)
async def request_pending_otp(
    request: Request,
    visit: tuple[MessagePatient, MessagePendingSession] = Depends(get_current_pending),
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    """Send the code that will turn this conversation into an account.

    THE BODY IS EMPTY AND THAT IS THE POINT. A pending token proves only "this browser opened
    this conversation"; letting it also NAME the inbox to be mailed would make it a way to
    send a code anywhere. The address comes from the service leg
    (`POST /internal/brain-message/pending-email`) or the route refuses.

    Same OTP rules as the account login — the same generator, the same 10-minute expiry, the
    same per-attempt ceiling, and both limiters (per-IP for the service, per address for the
    inbox a distributed attacker would flood).
    """
    _, pending = visit
    if pending.email is None:
        raise HTTPException(status.HTTP_409_CONFLICT, _PENDING_EMAIL_MISSING)
    address = pending.email
    if not _ip_limiter.allow(client_ip(request)) or not _email_limiter.allow(address):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")

    code = await patient_access.issue_pending_otp(session, pending)
    if code is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _OTP_INVALID)
    # Fail-soft, like every transactional e-mail in this repo.
    await secretaria_provisioning.send_notification_email(
        address,
        _OTP_EMAIL_TEMPLATE,
        {"code": code, "ttl_minutes": get_settings().PATIENT_OTP_EXPIRE_MINUTES},
    )
    logger.info("patient_pending_otp_requested", tenant_id=str(pending.tenant_id))
    return MessageOut(detail=_OTP_REQUEST_MESSAGE)


@router.post(
    "/pending/verify-otp",
    response_model=PatientAccountOut,
    summary="Turn the pending conversation into a real account session",
    responses={
        400: {"description": "Unknown, wrong, expired or already-used code."},
        401: {"description": "Missing or invalid pending token."},
        409: {"description": "This conversation has not captured an e-mail yet."},
        429: {"description": "Rate limited (per-IP verify budget)."},
    },
)
async def verify_pending_otp(
    payload: PendingVerifyIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    visit: tuple[MessagePatient, MessagePendingSession] = Depends(get_current_pending),
) -> PatientAccountOut:
    """Burn the code, open the account, and KEEP THE HANDLE this conversation was born with.

    The order is the whole feature, and each step is the account model's own code:

    1. the clinic must still have the channel on — checked BEFORE the code is spent, exactly
       as the deprecated `tenant_id` body does, so a closed clinic never burns a good code;
    2. `verify_account_otp` on the CAPTURED address (never one from this body);
    3. `open_account` — the address's account, created if this is its first;
    4. `adopt_pending_identity` — the visitor's handle takes the address, so `patient_ref` is
       the SAME before and after the code and the appointment booked minutes ago stays where
       secretarIA put it. It declines only when this clinic ALREADY had an identity for the
       address; then that row is the account's, this conversation's handle keeps its own id
       (nothing is merged, ever) and the visit records the swap in `superseded_by`;
    5. `add_clinic` — the clinic enters the account, and the `brain_message_channel_access`
       consent is recorded exactly ONCE by the same `WHERE NOT EXISTS` every other path uses;
    6. the full account session replaces the visit: account cookie set, pending cookie
       cleared, pending row closed.

    What comes back is the ordinary `verify-otp` body, so a client that already speaks the
    account contract needs nothing new. When step 4 declined, the top-level `patient_ref` is
    the clinic's pre-existing handle and differs from the pending one — that difference IS the
    signal to switch threads.
    """
    _, pending = visit
    if not _verify_limiter.allow(client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    if pending.email is None:
        raise HTTPException(status.HTTP_409_CONFLICT, _PENDING_EMAIL_MISSING)

    if pending.verified_at is None:
        verified = await patient_access.verify_pending_identity(session, pending, payload.code)
    else:
        verified = None
    if pending.verified_at is None and verified is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _OTP_INVALID)

    completed = await patient_access.complete_pending_identity(session, pending)
    if completed is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _OTP_INVALID)
    account, canonical, raw_session, session_id = completed
    set_patient_session_cookie(response, raw_session)
    clear_patient_pending_cookie(response)
    # The clinic and whether the handle survived — never the address, never a token.
    logger.info(
        "patient_pending_session_verified",
        tenant_id=str(pending.tenant_id),
        handle_kept=pending.superseded_by is None,
    )
    return await _account_body(
        session, account, session_id, canonical, invited_tenant_id=canonical.tenant_id
    )
