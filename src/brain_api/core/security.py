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


def create_access_token(*, sub: str, tenant_id: str | None, role: str) -> str:
    """Mint a short-lived access token. `sub` is the brain user id (UUID string)."""
    settings = get_settings()
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": sub,  # user id — stable server-side identity
        "tenant_id": tenant_id,  # which tenant the user acts for (None for admin)
        "role": role,  # admin | tenant_owner | tenant_staff
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
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


def create_hub_token(*, tenant_id: str, actor_user_id: str) -> str:
    """Mint the tenant-scoped token the doctor portal presents to secretarIA's hub.

    Claims carry the TENANT (`sub`) plus `scope=secretaria_hub` and the acting user for
    audit (`act`) — deliberately no `role`/`tenant_id` claims, so it can never pass as a
    brain user JWT (and `get_current_principal` rejects any `scope`-bearing token
    outright). secretarIA never validates it locally: it introspects via brain-api's
    /internal surface, which re-reads the LIVE entitlement (auth-jwt-multitenant: the
    mutable "is this paid?" state is never trusted from a token).
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
