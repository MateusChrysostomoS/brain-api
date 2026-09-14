"""Patient-access router — the ONLY browser-facing surface a patient can reach.

THE TRUST BOUNDARY, SPELLED OUT ONCE AND ENFORCED IN CODE (auth-jwt-multitenant):

  browser (patient)  --->  brain-api      : `Authorization: Bearer <scoped patient JWT>`
                                            + `__Host-patient_session` cookie
  brain-api          --->  secretarIA     : `X-Internal-Api-Key`  (service-pair secret)
  brain-api          --->  PreCheck       : `X-Internal-Token`    (service-pair secret)

The two internal keys are SYMMETRIC secrets shared by exactly two services. They must
NEVER authenticate an end user — not staff, not a patient. A browser that could present
one would be authenticated as brain-api itself, i.e. as every tenant at once. That is why
the patient's own leg uses a purpose-scoped JWT instead, and why `get_current_patient`
below refuses anything that is not one.

The reverse also holds and is already enforced upstream: `api/deps.py::get_current_principal`
rejects ANY token carrying a `scope` claim, so the token minted here cannot open a staff
route even by accident. Neither direction depends on a reviewer noticing.
"""

from datetime import UTC, datetime, timedelta
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
    clear_patient_session_cookie,
    read_patient_session_cookie,
    require_client_header,
    set_patient_session_cookie,
)
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.core.ratelimit import SlidingWindowLimiter, client_ip
from brain_api.core.security import create_patient_token, decode_patient_token
from brain_api.models import Tenant
from brain_api.models.patient_access import MessagePatient, MessagePatientSession
from brain_api.schemas.patient_access import (
    ClinicSessionOut,
    ConfirmSiblingIn,
    MessageOut,
    OtpRequestIn,
    OtpVerifyIn,
    PatientMessageIn,
    PatientRefreshOut,
    PatientSessionOut,
    RelayOut,
    SiblingCandidateOut,
    ThreadListOut,
    ThreadOut,
)
from brain_api.services import message_switchboard, patient_access, secretaria_provisioning
from brain_api.services.entitlements import resolve_entitlement

logger = get_logger(__name__)

router = APIRouter(prefix="/patient-access", tags=["patient-access"])

# Three buckets, three abuse cases (see config.py for why they are not one).
_ip_limiter = SlidingWindowLimiter(
    "patient_otp_ip", lambda: get_settings().PATIENT_OTP_RATE_LIMIT_PER_MIN
)
_email_limiter = SlidingWindowLimiter(
    "patient_otp_email", lambda: get_settings().PATIENT_OTP_EMAIL_RATE_LIMIT_PER_MIN
)
_verify_limiter = SlidingWindowLimiter(
    "patient_otp_verify", lambda: get_settings().PATIENT_VERIFY_RATE_LIMIT_PER_MIN
)
# A fourth bucket, for the multi-clinic account: confirming a sibling clinic is the one
# AUTHENTICATED route that mints a session, so it is metered like verify — but keyed by the
# ACCOUNT (the authenticated address), not by IP. Behind the portal's same-origin proxy the
# first X-Forwarded-For hop is either client-controlled or the proxy's own address, so a
# per-IP key would be spoofable or one bucket for every patient; an authenticated key is
# neither (docs/CHECKPOINT_conta_unica_multi_clinica.md).
_link_limiter = SlidingWindowLimiter(
    "patient_account_link", lambda: get_settings().PATIENT_LINK_RATE_LIMIT_PER_MIN
)

# The e-mail template secretarIA renders. brain-api owns no SMTP of its own: every
# transactional e-mail in this repo is delegated to secretarIA's
# `/internal/notifications/email` (see api/auth.py's password reset, api/onboarding.py's
# professional invite). Reusing that path is what the "no new provider" decision meant.
_OTP_EMAIL_TEMPLATE = "patient_access_otp"

# The ONE answer `request-otp` ever gives. True whether or not the clinic exists, whether
# or not the channel is on, and whether or not anyone reads that inbox — so the endpoint
# cannot be used to discover which clinics are on Brain-Message.
_OTP_REQUEST_MESSAGE = "Se a clínica atender por aqui, enviamos um código para esse e-mail."
# Same message for a wrong code, an expired one, one already used, and one that never
# existed. Distinguishing them would confirm which addresses have a live challenge.
_OTP_INVALID = "Código inválido ou expirado"
# Linking needs the login that the code opened, in THIS browser, recently. Every failure
# of that proof reads the same and means the same thing to the client: prove the address
# again, then retry.
_REAUTH_REQUIRED = "reauthentication_required"
# One answer for every reason a sibling does not exist for this account.
_SIBLING_NOT_FOUND = "sibling_not_found"


async def _authenticate_patient(
    authorization: str | None, session: AsyncSession
) -> tuple[MessagePatient, MessagePatientSession] | None:
    """The live identity + session row behind a patient bearer token, or `None`.

    Three checks, all fail-closed and indistinguishable to the caller:

    1. the token decodes AND carries exactly `scope=patient_message` plus a `sid`
       (`decode_patient_token` refuses a staff or hub token, and a pre-`sid` one);
    2. the session row it names is live — not revoked, not expired — and belongs to the
       same patient and tenant the token claims, and so is the LOGIN it descends from
       (`login_sid`, which differs from `sid` only for a clinic linked by confirmation).
       This is what makes a logout end the access leg too, not only the cookie (OWASP
       ASVS 5.0 7.4.1), and ending a login end the clinics it opened;
    3. the identity still exists with that SAME tenant. The tenant is re-read from the
       rows, never trusted from the claim — a token is a fact about the past, and this is
       the auth-jwt-multitenant rule that mutable state is looked up server-side.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    claims = decode_patient_token(authorization[7:].strip())
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
    if row is None or row.patient_id != patient_ref or row.tenant_id != tenant_id:
        return None
    if login_session_id != session_id and (
        await patient_access.find_live_session(session, login_session_id) is None
    ):
        # A linked clinic's session lives only as long as the login that opened it — the
        # guard that also catches a confirmation whose insert landed after a logout.
        return None
    patient = await session.get(MessagePatient, patient_ref)
    if patient is None or patient.tenant_id != tenant_id:
        # A patient deleted, or a token whose tenant no longer matches the row: refuse.
        # Answering with the ROW's tenant instead would let a stale token follow an
        # identity that was moved.
        return None
    return patient, row


async def _sibling_report(
    session: AsyncSession, patient: MessagePatient
) -> tuple[list[tuple[MessagePatient, Tenant]], set[UUID]]:
    """The multi-clinic account's discovery step, shared by `verify_otp` and `refresh`:
    which other clinics know this proven address, and which of those the account already
    confirmed. Reads only — nothing is minted here."""
    siblings = await patient_access.find_sibling_candidates(
        session, patient.email, exclude_tenant_id=patient.tenant_id
    )
    linked = await patient_access.linked_identities(session, [p for p, _ in siblings])
    return siblings, linked


def _candidates_out(
    siblings: list[tuple[MessagePatient, Tenant]], linked: set[UUID]
) -> list[SiblingCandidateOut]:
    return [
        SiblingCandidateOut(
            tenant_id=tenant.id,
            clinic_name=tenant.clinic_name,
            already_linked=sibling.id in linked,
        )
        for sibling, tenant in siblings
    ]


def _expired_patient_cookie_headers() -> dict[str, str]:
    """The `Set-Cookie` that deletes the patient cookie, as raise-able headers.

    `api/auth.py::_expired_cookie_headers`'s twin, for the same reason: FastAPI discards
    the injected `Response` when a route RAISES, so a rejection that must also expire the
    cookie has to carry the header on the `HTTPException` itself — built off a throwaway
    `Response` so `core/cookies.py` stays the one source of the cookie's attributes.
    """
    probe = Response()
    clear_patient_session_cookie(probe)
    return {"set-cookie": probe.headers["set-cookie"]}


async def get_current_patient(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> MessagePatient:
    """Turn a scoped patient bearer token into the live `MessagePatient` row, or 401.

    The checks live in `_authenticate_patient`. The tenant every thread route acts on is
    the one returned here — derived from the validated token and its rows, never from a
    header, a query string, a cookie or a body — which is why a session minted for a
    linked clinic opens those routes unchanged, and only for that clinic.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    found = await _authenticate_patient(authorization, session)
    if found is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return found[0]


async def get_current_patient_login(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> tuple[MessagePatient, MessagePatientSession]:
    """`get_current_patient`, plus the session row the token is bound to — for the one
    route that has to reason about the session itself (`confirm_sibling`)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    found = await _authenticate_patient(authorization, session)
    if found is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return found


@router.post(
    "/request-otp",
    response_model=MessageOut,
    summary="E-mail a login code to a patient",
    description=(
        "Sends a short code to the address. ALWAYS returns the same 200 body — whether "
        "or not the clinic exists, has Brain-Message enabled, or knows the address."
    ),
    responses={429: {"description": "Rate limited (per-IP and per-address budgets)."}},
)
async def request_otp(
    payload: OtpRequestIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    """Begin the patient login.

    TWO limiters, checked before any DB work. The per-IP bucket protects the service; the
    per-ADDRESS bucket protects the person whose inbox a distributed attacker would
    otherwise flood — a case the per-IP one structurally cannot see. Both fail open
    internally (`SlidingWindowLimiter`), so neither can 500 the login path.

    Enumeration: there is exactly ONE return, and the failing branch does less work than
    the succeeding one, so the timing is not perfectly flat — the same known limitation
    recorded on `/auth/password-reset/request`, stated rather than papered over.
    """
    address = patient_access.normalize_email(payload.email)
    if not _ip_limiter.allow(client_ip(request)) or not _email_limiter.allow(address):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")

    code = await patient_access.issue_otp(session, payload.tenant_id, address)
    if code is not None:
        # Fail-soft, like every transactional e-mail in this repo: a bounced send must not
        # become a 500 that tells the caller this clinic exists.
        await secretaria_provisioning.send_notification_email(
            address,
            _OTP_EMAIL_TEMPLATE,
            {
                "code": code,
                "ttl_minutes": get_settings().PATIENT_OTP_EXPIRE_MINUTES,
            },
        )
        # Tenant only — never the address, and above all never `code`.
        logger.info("patient_otp_requested", tenant_id=str(payload.tenant_id))
    else:
        logger.info("patient_otp_requested_no_channel")
    return MessageOut(detail=_OTP_REQUEST_MESSAGE)


@router.post(
    "/verify-otp",
    response_model=PatientSessionOut,
    summary="Exchange a code for a patient session",
    responses={
        400: {"description": "Unknown, wrong, expired or already-used code."},
        429: {"description": "Rate limited (per-IP verify budget)."},
    },
)
async def verify_otp(
    payload: OtpVerifyIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> PatientSessionOut:
    """Burn the code, mint the identity, open the session, record the consent.

    Order matters and is deliberate: the consent event is written INSIDE the same
    transaction that creates the identity (`services/patient_access.py::_resolve_patient`),
    so an identity can never exist without its LGPD row. Session issuance comes after and
    commits separately — a failure there costs the patient a retry, not their consent
    trail.

    Both legs of the session are issued here: the opaque revocable token as the
    `__Host-patient_session` HttpOnly cookie (unreadable by page JavaScript), and the
    short scoped JWT in the body for the client to hold IN MEMORY. Same split, same
    reasoning as the staff session — one XSS must not yield a long-lived credential.

    It also names the other clinics where this address already is a patient, as
    `sibling_candidates` WITHOUT tokens: discovery happens here, access only through
    `confirm_sibling`, one named clinic at a time.
    """
    if not _verify_limiter.allow(client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")

    patient = await patient_access.verify_otp(
        session, payload.tenant_id, payload.email, payload.code
    )
    if patient is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _OTP_INVALID)

    raw_session, session_id = await patient_access.issue_patient_session(session, patient)
    set_patient_session_cookie(response, raw_session)
    settings = get_settings()
    logger.info(
        "patient_session_issued",
        tenant_id=str(patient.tenant_id),
        patient_ref=str(patient.id),
    )

    # The multi-clinic account, AFTER the unchanged session above: which other clinics is
    # this just-proven address already a patient of? Only asked about — nothing is minted
    # for them here, because the address alone must never authorize a link.
    clinic = await session.get(Tenant, patient.tenant_id)
    siblings, linked = await _sibling_report(session, patient)
    # Tenant and a count only — never the address, never a clinic's name.
    logger.info(
        "patient_sibling_candidates_found",
        tenant_id=str(patient.tenant_id),
        count=len(siblings),
    )
    return PatientSessionOut(
        access_token=create_patient_token(
            tenant_id=str(patient.tenant_id),
            patient_ref=str(patient.id),
            session_id=str(session_id),
            # A login descends from no other login.
            login_session_id=str(session_id),
        ),
        expires_in=settings.PATIENT_TOKEN_EXPIRE_MINUTES * 60,
        tenant_id=patient.tenant_id,
        patient_ref=patient.id,
        clinic_name=clinic.clinic_name if clinic is not None else "",
        sibling_candidates=_candidates_out(siblings, linked),
    )


@router.post(
    "/refresh",
    response_model=PatientRefreshOut,
    summary="Renew the patient session silently — no code, no prompt",
    description=(
        "Reads the `__Host-patient_session` cookie and answers with a fresh access token "
        "for the login clinic plus one for every clinic this account already linked. The "
        "cookie is rotated and its lifetime slides forward; presenting the value it replaced "
        "after a short grace window ends the whole account (reuse signal)."
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
) -> PatientRefreshOut:
    """The silent renewal behind "opened the portal, already logged in".

    The cookie is the ONLY credential here — no bearer, because the whole point is that
    the page just reloaded and holds nothing in memory. That makes it the second ambient
    credential in this service after `/auth/refresh`, and it gets the same guard for the
    same reason: `require_client_header` runs BEFORE the token is spent, so a cross-site
    form POST cannot burn the patient's cookie (auth-jwt-multitenant, "check it before
    spending the token").

    What comes back is `verify-otp`'s body plus `linked_sessions` — a token per clinic the
    account already confirmed (§2.5 of the prompt: a linked clinic must survive a reload
    without a new confirmation or code). Reopening them rests on the recorded consent, not
    on a recent code, so it has no `PATIENT_LINK_CONFIRM_WINDOW_MINUTES` gate; linking a
    NEW clinic still does (`confirm_sibling`). The `login_sid` of each reissued token is
    the SAME row id as before the refresh, because `rotate_patient_session` renews the row
    in place — which is also why a linked token minted before the refresh keeps working.

    A cookie that failed is expired in the browser on the way out (`_expired_patient_
    cookie_headers`): left in place it would make every future boot start with a doomed
    refresh, and after a reuse-triggered revocation it is precisely the value to distrust.
    """
    raw = read_patient_session_cookie(request)
    if raw is None:
        # Nothing to clear: no cookie was sent.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing patient session")
    require_client_header(request)

    renewal = await patient_access.rotate_patient_session(session, raw)
    if renewal is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired session",
            headers=_expired_patient_cookie_headers(),
        )
    patient, login_row = renewal.patient, renewal.row
    if renewal.new_raw_token is not None:
        set_patient_session_cookie(response, renewal.new_raw_token)
    settings = get_settings()

    clinic = await session.get(Tenant, patient.tenant_id)
    siblings, linked = await _sibling_report(session, patient)
    linked_sessions: list[ClinicSessionOut] = []
    for sibling, tenant in siblings:
        if sibling.id not in linked:
            continue
        sibling_session_id = await patient_access.reopen_linked_clinic(session, sibling)
        linked_sessions.append(
            ClinicSessionOut(
                access_token=create_patient_token(
                    tenant_id=str(sibling.tenant_id),
                    patient_ref=str(sibling.id),
                    session_id=str(sibling_session_id),
                    login_session_id=str(login_row.id),
                ),
                expires_in=settings.PATIENT_TOKEN_EXPIRE_MINUTES * 60,
                tenant_id=sibling.tenant_id,
                clinic_name=tenant.clinic_name,
                patient_ref=sibling.id,
            )
        )
    # Ids and counts only — never the address, never a token.
    logger.info(
        "patient_session_refreshed",
        tenant_id=str(patient.tenant_id),
        patient_ref=str(patient.id),
        rotated=renewal.new_raw_token is not None,
        linked_count=len(linked_sessions),
    )
    return PatientRefreshOut(
        access_token=create_patient_token(
            tenant_id=str(patient.tenant_id),
            patient_ref=str(patient.id),
            session_id=str(login_row.id),
            login_session_id=str(login_row.id),
        ),
        expires_in=settings.PATIENT_TOKEN_EXPIRE_MINUTES * 60,
        tenant_id=patient.tenant_id,
        patient_ref=patient.id,
        clinic_name=clinic.clinic_name if clinic is not None else "",
        sibling_candidates=_candidates_out(siblings, linked),
        linked_sessions=linked_sessions,
    )


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

    Two scopes, chosen by what the caller proves:

    - WITH a valid patient bearer: the whole ACCOUNT ends — every live session of the
      authenticated address, at every clinic and on every device
      (`revoke_account_sessions`). A multi-clinic "sair" that left the other clinics'
      sessions alive would not be a logout (OWASP ASVS 5.0 7.4.1), and because every
      access token is bound to its row by `sid`, those tokens die too.
    - WITHOUT one (or with a dead one): only the session in this browser's cookie, exactly
      as before — the fallback a single-clinic client already relies on.

    Never refused, on purpose: a logout that could fail is a logout a patient cannot
    trust, and the worst a forged one achieves is signing someone out — which is also why
    there is no CSRF header here (auth-jwt-multitenant: "Do NOT check it on logout").

    ORDER MATTERS: the bearer is resolved BEFORE the cookie's row is revoked. In the
    common case both name the same session, and revoking it first would make the bearer
    look dead and quietly shrink an account logout into a device logout.
    """
    authenticated = await _authenticate_patient(authorization, session)

    raw = read_patient_session_cookie(request)
    if raw:
        found = await patient_access.find_patient_session(session, raw)
        if found is not None:
            await patient_access.revoke_patient_session(session, found[0])

    if authenticated is not None:
        patient = authenticated[0]
        count = await patient_access.revoke_account_sessions(session, patient.email)
        # Tenant and a count only — never the address.
        logger.info(
            "patient_account_sessions_revoked",
            tenant_id=str(patient.tenant_id),
            count=count,
        )
    clear_patient_session_cookie(response)
    return MessageOut(detail="Sessão encerrada")


@router.post(
    "/siblings/{tenant_id}/confirm",
    response_model=ClinicSessionOut,
    summary="Confirm, by name, that another clinic's conversation is yours",
    description=(
        "Links a clinic from `verify-otp`'s `sibling_candidates` to the account and opens "
        "a session there without a new code. Needs the bearer of the session the code "
        "opened AND that session's cookie, shortly after the code. Idempotent."
    ),
    responses={
        401: {
            "description": (
                "Missing/invalid token, a token not bound to this browser's login, or a "
                "login too old to link (`reauthentication_required`)."
            )
        },
        404: {"description": "No such sibling for this account — one answer for every reason."},
        422: {"description": "The body names a different clinic than the path."},
        429: {"description": "Rate limited (per-account link budget)."},
    },
)
async def confirm_sibling(
    payload: ConfirmSiblingIn,
    request: Request,
    tenant_id: UUID,
    login: tuple[MessagePatient, MessagePatientSession] = Depends(get_current_patient_login),
    session: AsyncSession = Depends(get_session),
) -> ClinicSessionOut:
    """The explicit act that turns a discovered clinic into a session.

    Why each gate is here (TECH skill `cross-tenant-account-linking`):

    1. A valid patient bearer, whose ADDRESS is the only thing the lookup is scoped by.
       The path picks which clinic, never whose identity: another address's patient at
       that clinic is unreachable, and so is a clinic this address never verified at.
    2. This browser's `__Host-patient_session` cookie must resolve to the very session the
       bearer is bound to. The bearer lives in page memory and is the easy leg to copy;
       the cookie is HttpOnly. Needing both keeps a leaked access token from being turned
       into sessions at other clinics from another device — and, since the flat cookie
       holds the LOGIN session, a linked clinic's token cannot chain further links.
    3. That login must be recent (`PATIENT_LINK_CONFIRM_WINDOW_MINUTES`): linking changes
       what one proof reaches, which OWASP ASVS 5.0 7.5.1 treats as needing fresh
       authentication.
    4. The body must name the same clinic as the path (`ConfirmSiblingIn`).
    5. A per-account budget (`_link_limiter`), counted only once 1–3 hold — so a leaked
       bearer replayed without the cookie cannot spend the patient's budget.
    6. The linked token names this login as its `login_sid`, so it dies with the login —
       a logout that commits between the checks above and the insert below included.

    Then the consent event is written (once — a replay records nothing) and committed
    BEFORE the session is issued, `verify-otp`'s own order. One 404 answers every "no such
    sibling" reason, so the route cannot tell anyone which clinics know an address.
    """
    if payload.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "tenant_mismatch")
    patient, login_row = login

    raw = read_patient_session_cookie(request)
    cookie_session = await patient_access.find_patient_session(session, raw) if raw else None
    if cookie_session is None or cookie_session[0].id != login_row.id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _REAUTH_REQUIRED)
    settings = get_settings()
    if not patient_access.session_is_recent(
        login_row, settings.PATIENT_LINK_CONFIRM_WINDOW_MINUTES
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _REAUTH_REQUIRED)
    # Counted only now: a bearer that fails the cookie or recency check above must not be
    # able to spend the account's budget and lock the real patient out of linking.
    if not _link_limiter.allow(patient.email):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")

    linked = await patient_access.confirm_sibling_link(session, patient, tenant_id)
    if linked is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _SIBLING_NOT_FOUND)
    sibling, clinic = linked

    # The linked row's opaque token is dropped on purpose: the cookie is flat and stays the
    # login's. The row exists so the JWT below has something to be revoked through, which
    # is also why it lives exactly as long as that JWT and not a login's 30 days.
    _, sibling_session_id = await patient_access.issue_patient_session(
        session,
        sibling,
        lifetime=timedelta(minutes=settings.PATIENT_TOKEN_EXPIRE_MINUTES),
    )
    return ClinicSessionOut(
        access_token=create_patient_token(
            tenant_id=str(sibling.tenant_id),
            patient_ref=str(sibling.id),
            session_id=str(sibling_session_id),
            login_session_id=str(login_row.id),
        ),
        expires_in=settings.PATIENT_TOKEN_EXPIRE_MINUTES * 60,
        tenant_id=sibling.tenant_id,
        clinic_name=clinic.clinic_name,
        patient_ref=sibling.id,
    )


@router.get(
    "/threads",
    response_model=ThreadListOut,
    summary="Which product threads this patient may open",
    responses={401: {"description": "Missing or invalid patient session."}},
)
async def list_threads(
    patient: MessagePatient = Depends(get_current_patient),
    session: AsyncSession = Depends(get_session),
) -> ThreadListOut:
    """Exactly `EntitlementOut.products` intersected with `channels.brain_message`.

    An EMPTY list, not a 403, when the channel is off: the patient holds a valid session
    (the clinic may have switched the channel off after they logged in) and the honest
    answer is "there is nothing here", which is also what the client renders. The 403 for
    a channel-off tenant lives on the per-thread routes below, where the patient is
    actually trying to DO something.
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
    patient: MessagePatient = Depends(get_current_patient),
    session: AsyncSession = Depends(get_session),
) -> RelayOut:
    """Relay to the named product's internal inbound endpoint.

    `tenant_id` and the patient handle come from the SESSION, never from the body or the
    path — which is the whole tenant-isolation story for this route: there is no input a
    patient could point at another clinic, so cross-tenant delivery is not something the
    code has to catch, it is something it cannot express.

    `require_product` runs BEFORE any upstream call, so asking `precheck` of a clinic that
    only bought `secretaria` costs zero network and leaks nothing about what it did buy.
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
    patient: MessagePatient = Depends(get_current_patient),
    session: AsyncSession = Depends(get_session),
) -> RelayOut:
    """Poll the product's transcript for this patient.

    `since` is passed through as an opaque string rather than parsed into a datetime here:
    the two backends define their own cursor (secretarIA compares `Message.created_at`,
    PreCheck an `answered_at`), and re-formatting a value this service does not own is how
    a cursor silently starts skipping a row. Both bind it as a parameter on their side.

    Polling is the whole read path in this first version — see
    `services/message_switchboard.py::list_messages` for why, and why a push channel is a
    future optimization rather than a missing requirement.
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
