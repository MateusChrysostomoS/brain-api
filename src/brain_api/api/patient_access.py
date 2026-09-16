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

from datetime import UTC, datetime
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
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
    PatientMessageIn,
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


async def get_thread_patient(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> MessagePatient:
    """The identity behind EITHER a clinic token or a pending token, or 401.

    The thread routes take both because a thread is the one thing both populations do: a
    logged-in patient of a clinic, and a visitor still in the conversation that will become
    one. Everything those routes need — `tenant_id` and the handle — comes off the identity
    either way, and neither token can name a clinic it was not minted for.

    Order matters only for cost: a clinic token is the common case, and a pending token fails
    `decode_patient_token` without a query.
    """
    if _bearer_value(authorization) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    found = await _authenticate_patient(authorization, session)
    if found is not None:
        return found[0]
    pending = await _authenticate_pending(authorization, session)
    if pending is not None:
        return pending[0]
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")


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
    login: tuple[MessagePatientAccount, MessagePatientSession] = Depends(get_current_account),
    session: AsyncSession = Depends(get_session),
) -> ClinicSessionOut:
    """Increment the account with the clinic the invite names.

    No code and no recent-login window, by the owner's decision ("apenas incrementar"). The
    risk accepted, stated: whoever holds a live account token (30 minutes, page memory) can
    add clinics to that account and receive their tokens — the same reach page memory
    already has, since every clinic token of the account lives there too, and nothing
    outside that account becomes readable. The inbox was proven by the login's code.
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
    return ThreadListOut(
        data=[ThreadOut(product=p, clinic_name=ent.clinic_name) for p in products]
    )


@router.post(
    "/threads/{product}/messages",
    response_model=RelayOut,
    summary="Send one message on a product thread",
    responses={
        401: {"description": "Missing or invalid patient session."},
        403: {"description": "The clinic does not offer that product on this channel."},
        502: {"description": "The product backend failed or is misconfigured."},
        503: {"description": "That product's leg of the mesh is unconfigured or degraded."},
    },
)
async def send_thread_message(
    payload: PatientMessageIn,
    product: str = Path(
        description="secretaria | precheck — chosen by the CLIENT, never inferred."
    ),
    patient: MessagePatient = Depends(get_thread_patient),
    session: AsyncSession = Depends(get_session),
) -> RelayOut:
    """Relay to the named product's internal inbound endpoint.

    `tenant_id` and the patient handle come from the SESSION, never from the body or the
    path: there is no input a patient could point at another clinic. `require_product` runs
    BEFORE any upstream call, so an unowned product costs zero network and leaks nothing.
    """
    ent = await resolve_entitlement(session, patient.tenant_id)
    message_switchboard.require_product(ent, product)

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
        description="Cursor: return only what the product recorded strictly after this instant.",
    ),
    patient: MessagePatient = Depends(get_thread_patient),
    session: AsyncSession = Depends(get_session),
) -> RelayOut:
    """Poll the product's transcript for this patient.

    `since` is passed through opaque: each backend defines its own cursor, and re-formatting
    a value this service does not own is how a cursor silently starts skipping a row.
    """
    ent = await resolve_entitlement(session, patient.tenant_id)
    message_switchboard.require_product(ent, product)

    result = await message_switchboard.list_messages(
        product,
        tenant_id=patient.tenant_id,
        patient_ref=str(patient.id),
        since=since,
    )
    return RelayOut(product=product, payload=result, at=datetime.now(UTC))


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
    return ClinicPublicOut(
        tenant_id=clinic.id, clinic_name=clinic.clinic_name, products=products
    )


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
    session: AsyncSession = Depends(get_session),
) -> PendingSessionOut:
    """The door a patient with no account walks through — for EITHER product.

    `product` is checked with `require_product`, the same function the relay uses, so a direct
    PreCheck link to a clinic without PreCheck is refused here rather than at the first
    message. There is deliberately NO booking gate: the owner closed that on 2026-09-16
    ("todos os produtos, sem gate"). Opening PreCheck automatically right after secretarIA
    confirms an appointment stays a proactive behaviour elsewhere — it is not a condition of
    access, and nothing in this file treats it as one.

    NOTHING IS PRE-CREATED UPSTREAM. PreCheck's conductor opens its own session on the first
    inbound message (`resolve_session` is idempotent by `session_ref`), so the only way to
    pre-create one here would be to relay a synthetic message the patient never sent. The
    handle is enough: the session is born on the first real turn.

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

    code = await patient_access.issue_account_otp(session, address)
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

    clinic = await patient_access.channel_open_tenant(session, pending.tenant_id)
    if clinic is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _OTP_INVALID)
    if not await patient_access.verify_account_otp(session, pending.email, payload.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _OTP_INVALID)

    account = await patient_access.open_account(session, pending.email)
    kept = await patient_access.adopt_pending_identity(session, pending, account.email)
    canonical = await patient_access.add_clinic(session, account, clinic)
    if canonical is None:  # pragma: no cover - unreachable: the address owns its account
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _OTP_INVALID)
    await patient_access.close_pending_session(session, pending, canonical=canonical)

    raw_session, session_id = await patient_access.issue_patient_session(
        session, account, opened_at=canonical
    )
    set_patient_session_cookie(response, raw_session)
    clear_patient_pending_cookie(response)
    # The clinic and whether the handle survived — never the address, never a token.
    logger.info("patient_pending_session_verified", tenant_id=str(clinic.id), handle_kept=kept)
    return await _account_body(
        session, account, session_id, canonical, invited_tenant_id=canonical.tenant_id
    )
