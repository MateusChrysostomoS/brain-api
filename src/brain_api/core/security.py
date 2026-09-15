"""JWT issuance/validation and password hashing (the identity-authority primitives).

Follows the auth-jwt-multitenant skill:
- HS256 with a symmetric SECRET_KEY shared with precheck (so precheck *could* verify a
  token brain-api minted — full SSO is deferred; see CONTRACTS.md §0).
- `algorithms` is PINNED on decode (never trust the token's own alg — algorithm
  confusion / `alg: none`).
- bcrypt for passwords. bcrypt silently truncates at 72 bytes — callers reject longer
  passwords upstream (the auth schema caps length).
- The token carries only stable identity (`sub`/`tenant_id`/`role`). Entitlements and
  secrets are NEVER in the token; they are looked up server-side.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from brain_api.config import get_settings

ALGORITHM = "HS256"

# Purpose scope carried by the secretarIA hub token (NOT a user JWT): secretarIA's hub
# introspects it via /internal/secretaria/hub-token/verify, and brain-api's own
# `get_current_principal` REJECTS any scoped token, so the two token populations can
# never cross surfaces.
HUB_TOKEN_SCOPE = "secretaria_hub"

# bcrypt work factor lives in the hash itself; passlib defaults to 12 rounds.
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd.verify(plain, hashed)
    except ValueError:
        # Malformed/empty hash on the row — treat as a failed verification, never raise.
        return False


def create_access_token(
    *,
    sub: str,
    tenant_id: str | None,
    role: str,
    professional_id: str | None = None,
    is_owner: bool = False,
    is_manager: bool = False,
) -> str:
    """Mint a short-lived access token. `sub` is the brain user id (UUID string).

    `professional_id` (the user's `users.professional_id`, a secretaria professional id
    carried BY VALUE — CONTRACT_onboarding_v1.md §0) is included as a claim ONLY when the
    user row actually has one; omitted otherwise, so an old/professional-less token shape
    is unchanged.

    `is_owner`/`is_manager` (role-taxonomy round) are ALWAYS present, defaulting false —
    unlike `professional_id` they are never omitted, so `api/deps.py`'s claim parsing can
    treat a missing claim (an old/legacy token) and an explicit false identically.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": sub,  # user id — stable server-side identity
        "tenant_id": tenant_id,  # which tenant the user acts for (None for admin)
        "role": role,  # admin | doctor | manager (legacy: tenant_owner | tenant_staff)
        "is_owner": is_owner,  # the clinic owner (role-taxonomy round)
        "is_manager": is_manager,  # "also a manager" (role-taxonomy round)
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    if professional_id:
        claims["professional_id"] = professional_id
    return jwt.encode(claims, settings.SECRET_KEY, algorithm=ALGORITHM)


def create_precheck_token(precheck_user_id: int) -> str:
    """Mint a token that PreCheck's OWN auth will accept (the SSO handoff).

    PreCheck validates with `jose.jwt.decode(token, SECRET_KEY, algorithms=["HS256"])`
    and then does `User.id == int(payload["sub"])` (see PreCheck `app/core/security.py`
    + `app/core/deps.py`). So the token must:

    - be signed HS256 with the SAME shared SECRET_KEY (the mesh secret — brain-api and
      the PreCheck backend MUST be deployed with an identical SECRET_KEY value);
    - carry `sub` = the INTEGER PreCheck user id, as a string (PreCheck casts it to int);
    - carry `exp`.

    PreCheck reads ONLY `sub` and `exp` — no role/tenant/scope. We therefore put nothing
    else identity-bearing in it (auth-jwt-multitenant: no secrets/entitlements in the
    token beyond what the verifier needs). `iat` is included for hygiene; PreCheck ignores
    it. The claim shape mirrors PreCheck's own `create_access_token` exactly.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(precheck_user_id),  # PreCheck casts to int(payload["sub"]) -> users.id
        "iat": now,
        "exp": now + timedelta(minutes=settings.PRECHECK_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(claims, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    """Return the claims, or None for any invalid/expired/forged token.

    `algorithms` is PINNED — never pass the token's own alg. During a SECRET_KEY
    rotation window, `SECRET_KEY_PREVIOUS` is accepted for VERIFICATION only (minting
    always uses the current key), so already-issued tokens survive the rotation
    (docs/key-rotation.md).
    """
    settings = get_settings()
    for key in (settings.SECRET_KEY, settings.SECRET_KEY_PREVIOUS):
        if not key:
            continue
        try:
            return jwt.decode(token, key, algorithms=[ALGORITHM])
        except JWTError:
            continue
    return None


# --- secretarIA hub token (purpose-scoped, tenant-bearing; NOT a user JWT) ------------


def create_hub_token(
    *, tenant_id: str, actor_user_id: str, professional_id: str | None = None
) -> str:
    """Mint the tenant-scoped token the doctor portal presents to secretarIA's hub.

    Claims carry the TENANT (`sub`) plus `scope=secretaria_hub` and the acting user for
    audit (`act`) — deliberately no `role`/`tenant_id` claims, so it can never pass as a
    brain user JWT (and `get_current_principal` rejects any `scope`-bearing token
    outright). secretarIA never validates it locally: it introspects via brain-api's
    /internal surface, which re-reads the LIVE entitlement (auth-jwt-multitenant: the
    mutable "is this paid?" state is never trusted from a token).

    `professional_id`, when the acting user has one, rides along so secretarIA's hub can
    scope a professional-specific view (config/calendar) without a second lookup — same
    "included only when set" convention as `create_access_token`.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": tenant_id,  # the tenant the hub session acts for
        "scope": HUB_TOKEN_SCOPE,
        "act": actor_user_id,  # audit: which doctor opened the hub session
        "iat": now,
        "exp": now + timedelta(minutes=settings.HUB_TOKEN_EXPIRE_MINUTES),
    }
    if professional_id:
        claims["professional_id"] = professional_id
    return jwt.encode(claims, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_hub_token(token: str) -> dict[str, Any] | None:
    """Validate a hub token: signature/expiry via `decode_token` + the exact scope.

    Returns None for user JWTs (no/other scope) and for anything forged or expired —
    the introspection endpoint then answers `active=false` (fail closed).
    """
    claims = decode_token(token)
    if claims is None or claims.get("scope") != HUB_TOKEN_SCOPE or "sub" not in claims:
        return None
    return claims


# --- Refresh tokens (opaque, hashed at rest, rotate-on-use) ----------------------------


def generate_refresh_token() -> str:
    """A high-entropy opaque token (the client-side value; only its hash is stored)."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw: str) -> str:
    """SHA-256 hex for storage/lookup. Fast is fine: the input is 64 random bytes
    (not a guessable password), so bcrypt-style stretching adds nothing here."""
    return hashlib.sha256(raw.encode()).hexdigest()


# --- Patient session token (purpose-scoped; a PATIENT, never a staff user) -------------

# Purpose scope carried by the Brain-Message patient session token. Like HUB_TOKEN_SCOPE
# above, its load-bearing property is what it does to `api/deps.py::get_current_principal`:
# that dependency rejects ANY token carrying a `scope` claim outright, so a patient token
# is structurally incapable of authenticating a staff route — it fails the staff gate
# before role, tenant or ownership is even looked at. This is the auth-jwt-multitenant
# boundary the whole vertical rests on, and it is worth stating twice: the internal
# service key (`X-Internal-Api-Key` / `X-Internal-Token`) is a SERVICE-PAIR secret and must
# never authenticate an end user, staff or patient; a browser reaching brain-api presents
# THIS instead.
PATIENT_TOKEN_SCOPE = "patient_message"


def create_patient_token(
    *, tenant_id: str, patient_ref: str, session_id: str, login_session_id: str
) -> str:
    """Mint the short-lived access token for one Brain-Message patient session.

    The claims are the entire authority the patient has: WHICH clinic (`tenant_id`),
    WHICH patient (`sub`) and WHICH server-side session (`sid`). There is no role, no
    professional id and no ownership claim — a patient has none of those, and omitting
    them means a future gate that reads one cannot accidentally read a default off a
    patient token.

    `sub` is `MessagePatient.id` as a string, the same handle secretarIA and PreCheck
    receive. Nothing derived from the e-mail is in the token: the address is personal
    data, and a JWT is base64, not encryption — anyone holding the token could read it.

    `sid` is the `message_patient_sessions.id` the token was minted with, and it is what
    makes a logout real for THIS leg too: `api/patient_access.py::get_current_patient`
    re-reads that row on every request and refuses the token once the row is revoked
    (OWASP ASVS 5.0 7.4.1: after logout "the application disallows any further use of
    the session"). Without it a 30-minute bearer outlives the logout that was meant to
    end it — and with a multi-clinic account, one logout would leave N of them alive.

    `login_sid` names the session the patient's CODE opened. Every clinic token minted since
    the account model (2026-09-15) names that login row as both `sid` and `login_sid`; a
    token issued before it may still name a per-clinic row as `sid` and the login as
    `login_sid`, and `get_current_patient` requires both rows to be live. So ending a login —
    by either kind of logout — ends every clinic it opened, including a link whose insert
    raced the logout's revoke. Required, never defaulted to `sid`: a linked token minted
    without it would silently outlive the login it came from.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": patient_ref,
        "tenant_id": tenant_id,
        "sid": session_id,
        "login_sid": login_session_id,
        "scope": PATIENT_TOKEN_SCOPE,
        "iat": now,
        "exp": now + timedelta(minutes=settings.PATIENT_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(claims, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_patient_token(token: str) -> dict[str, Any] | None:
    """Validate a patient token: signature/expiry via `decode_token` + the EXACT scope.

    Returns None for a staff JWT, for a hub token, and for anything forged or expired —
    fail closed. The scope equality is what stops the three token populations from
    crossing surfaces in either direction: a doctor's token has no `scope` and is
    rejected here, exactly as a patient's is rejected by `get_current_principal`.
    """
    claims = decode_token(token)
    if claims is None or claims.get("scope") != PATIENT_TOKEN_SCOPE:
        return None
    # `sid` and `login_sid` are required, not optional: a token that cannot name its rows
    # cannot be revoked, and accepting one (say, a pre-`sid` token still inside its 30
    # minutes at deploy time) would reopen exactly the hole the claims close. Cost: one
    # re-login.
    if not all(claims.get(key) for key in ("sub", "tenant_id", "sid", "login_sid")):
        return None
    return claims


# --- Patient ACCOUNT token (the account's own login, not a clinic's) --------------------

# E-mail + code open the ACCOUNT (2026-09-15); a clinic enters it only by invite. This token
# authenticates the routes that act on the account — adding a clinic
# (`POST /patient-access/clinics`) and ending it — and nothing else: its scope is not
# `patient_message`, so every thread route (`decode_patient_token`) refuses it, and like
# every scoped token it is refused by `api/deps.py::get_current_principal`.
PATIENT_ACCOUNT_TOKEN_SCOPE = "patient_account"


def create_patient_account_token(*, account_id: str, session_id: str) -> str:
    """Mint the short-lived token of one patient ACCOUNT login.

    `sub` is `message_patient_accounts.id`; `sid` is the login's session row, re-read on every
    request so a logout ends this leg too. No tenant and no e-mail: the account's clinics are
    looked up server-side, and the address is personal data a base64 JWT would expose.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": account_id,
        "sid": session_id,
        "scope": PATIENT_ACCOUNT_TOKEN_SCOPE,
        "iat": now,
        "exp": now + timedelta(minutes=settings.PATIENT_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(claims, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_patient_account_token(token: str) -> dict[str, Any] | None:
    """Validate an account token: signature/expiry, the EXACT account scope, `sub` and `sid`."""
    claims = decode_token(token)
    if claims is None or claims.get("scope") != PATIENT_ACCOUNT_TOKEN_SCOPE:
        return None
    if not all(claims.get(key) for key in ("sub", "sid")):
        return None
    return claims
