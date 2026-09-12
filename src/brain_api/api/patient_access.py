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
    clear_patient_session_cookie,
    read_patient_session_cookie,
    set_patient_session_cookie,
)
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.core.ratelimit import SlidingWindowLimiter, client_ip
from brain_api.core.security import create_patient_token, decode_patient_token
from brain_api.models.patient_access import MessagePatient
from brain_api.schemas.patient_access import (
    MessageOut,
    OtpRequestIn,
    OtpVerifyIn,
    PatientMessageIn,
    PatientSessionOut,
    RelayOut,
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


async def get_current_patient(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> MessagePatient:
    """Turn a scoped patient bearer token into the live `MessagePatient` row.

    Two checks, and the second is the one that matters: the token must decode AND carry
    exactly `scope=patient_message` (`decode_patient_token` refuses a staff or hub token),
    and the identity it names must still exist with the SAME tenant the token claims. The
    tenant is re-read from the ROW, never trusted from the claim — a token is a fact about
    the past, and this is the auth-jwt-multitenant rule that mutable state is looked up
    server-side.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    claims = decode_patient_token(authorization[7:].strip())
    if claims is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    try:
        patient_ref = UUID(str(claims["sub"]))
        tenant_id = UUID(str(claims["tenant_id"]))
    except (ValueError, KeyError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from None

    patient = await session.get(MessagePatient, patient_ref)
    if patient is None or patient.tenant_id != tenant_id:
        # A patient deleted, or a token whose tenant no longer matches the row: refuse.
        # Answering with the ROW's tenant instead would let a stale token follow an
        # identity that was moved.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return patient


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
    """
    if not _verify_limiter.allow(client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")

    patient = await patient_access.verify_otp(
        session, payload.tenant_id, payload.email, payload.code
    )
    if patient is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _OTP_INVALID)

    raw_session = await patient_access.issue_patient_session(session, patient)
    set_patient_session_cookie(response, raw_session)
    settings = get_settings()
    logger.info(
        "patient_session_issued",
        tenant_id=str(patient.tenant_id),
        patient_ref=str(patient.id),
    )
    return PatientSessionOut(
        access_token=create_patient_token(
            tenant_id=str(patient.tenant_id), patient_ref=str(patient.id)
        ),
        expires_in=settings.PATIENT_TOKEN_EXPIRE_MINUTES * 60,
        tenant_id=patient.tenant_id,
        patient_ref=patient.id,
    )


@router.post(
    "/logout",
    response_model=MessageOut,
    summary="End the patient session",
)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    """Revoke the server-side row and clear the cookie. Always 200.

    Idempotent and unauthenticated on purpose: the credential IS the cookie, and a logout
    that could fail is a logout a patient cannot trust. An absent or already-dead session
    is simply nothing to revoke.
    """
    raw = read_patient_session_cookie(request)
    if raw:
        found = await patient_access.find_patient_session(session, raw)
        if found is not None:
            await patient_access.revoke_patient_session(session, found[0])
    clear_patient_session_cookie(response)
    return MessageOut(detail="Sessão encerrada")


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
