"""Shared FastAPI auth dependencies (auth-jwt-multitenant skill).

The token is validated here into a `Principal` (the stable identity from the JWT).
Mutable/sensitive state (the user row, tenant, entitlements) is looked up server-side
in the services — never trusted from the token.
"""

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from brain_api.core.security import decode_token
from brain_api.models.user import (
    ROLE_DOCTOR,
    ROLE_MANAGER,
    ROLE_SECRETARY,
    ROLE_TENANT_OWNER,
    ROLE_TENANT_STAFF,
)

# LEGACY (pre role-taxonomy round): a doctor-portal token minted before the deploy that
# introduced `doctor`/`manager` still carries one of these role strings for up to its
# ~30min TTL. Every doctor-scoped gate below accepts them alongside the new roles; remove
# once the transition window has long passed (see migrations/versions/0012_role_taxonomy.py).
_LEGACY_DOCTOR_ROLES = (ROLE_TENANT_OWNER, ROLE_TENANT_STAFF)


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: UUID | None
    role: str
    # Secretaria professional id carried BY VALUE (no FK — cross-service reference,
    # CONTRACT_onboarding_v1.md §0). None for a user with no professional linkage, or
    # when the claim is malformed (never raises — see the parse below).
    professional_id: UUID | None = None
    # Role-taxonomy round: booleans parsed from the `is_owner`/`is_manager` claims.
    # Absent/malformed on an old (legacy) token -> False, NEVER a 401 — the token's core
    # identity (sub/tenant_id/role) is otherwise valid.
    is_owner: bool = False
    is_manager: bool = False


def get_current_principal(authorization: str | None = Header(default=None)) -> Principal:
    """Turn an `Authorization: Bearer <jwt>` header into a validated Principal."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    claims = decode_token(authorization[7:].strip())
    if claims is None or "sub" not in claims:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    if claims.get("scope"):
        # Purpose-scoped service tokens (e.g. the secretarIA hub token) are NOT user
        # sessions — they must never authenticate a browser-facing route.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    tid = claims.get("tenant_id")
    raw_professional_id = claims.get("professional_id")
    professional_id: UUID | None = None
    if raw_professional_id:
        try:
            professional_id = UUID(str(raw_professional_id))
        except ValueError:
            # A malformed claim is treated as absent, never a 401 — the token's identity
            # (sub/tenant_id/role) is otherwise valid.
            professional_id = None
    return Principal(
        user_id=claims["sub"],
        tenant_id=UUID(tid) if tid else None,
        role=claims.get("role", ""),
        professional_id=professional_id,
        # `bool(...)` tolerates any truthy/falsy claim shape and a missing claim (a
        # legacy pre-taxonomy token) reads as False — never a 401.
        is_owner=bool(claims.get("is_owner", False)),
        is_manager=bool(claims.get("is_manager", False)),
    )


def require_role(*allowed: str):
    """Dependency factory: 403 unless the caller's role is allowed."""

    def _dep(p: Principal = Depends(get_current_principal)) -> Principal:
        if p.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return p

    return _dep


def require_tenant(p: Principal = Depends(get_current_principal)) -> Principal:
    """Require a tenant-scoped principal (a token that carries a tenant_id).

    Platform `admin` tokens have no tenant context — endpoints that resolve
    per-tenant state (e.g. GET /entitlements) reject them with 409.
    """
    if p.tenant_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No tenant in context")
    return p


# `manager` gets every gate `doctor` gets (product decision: a manager has full doctor
# access, semantically the clinic's gestor) — the two are interchangeable for every
# tenant-scoped route below; only owner-only actions further gate on `p.is_owner`.
# `secretary` (secretary round, 2026-08-14) joins them at the ROUTER level: the clinic's
# human receptionist runs the same operational portal. It is NOT a doctor synonym though
# — the three PreCheck/professional exclusions are enforced per-route by
# `deny_secretary` below, never by this tuple.
DOCTOR_ROLES = (ROLE_DOCTOR, ROLE_MANAGER, ROLE_SECRETARY)


def require_doctor(p: Principal = Depends(get_current_principal)) -> Principal:
    """Require a tenant-scoped user of the operational clinic portal (`doctor`,
    `manager` or `secretary`; LEGACY: `tenant_owner`/`tenant_staff` on a not-yet-expired
    pre-taxonomy token).

    Despite the name (kept for continuity — it gates the `/doctor/*` URL space, which is
    the portal's prefix, not a claim about the caller being a physician) this is the
    "tenant-scoped operational portal" gate, not a clinical one. Since the secretary
    round it admits the clinic's human receptionist too; the routes that must NOT follow
    that widening call `deny_secretary` explicitly.

    Platform `admin` tokens are rejected with 403 — admins use `/admin/*`, not the doctor
    portal (RBAC task: "/doctor/* routes return 403 for admin tokens, wrong portal"). A
    doctor role always carries a `tenant_id`; its absence is a malformed principal, also
    403. The route then scopes purely by `p.tenant_id` (never a client-supplied id).
    """
    if p.role not in (*DOCTOR_ROLES, *_LEGACY_DOCTOR_ROLES) or p.tenant_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Doctor access required")
    return p


def require_owner(p: Principal = Depends(require_doctor)) -> Principal:
    """Require the doctor-scoped principal to be the tenant OWNER — or a `secretary`.

    Layers on top of `require_doctor` (still 403 for admin / tenant-less tokens). Owner is
    now `p.is_owner` (role-taxonomy round) rather than a role string; a LEGACY token
    (pre-taxonomy, no `is_owner` claim) falls back to its old role check so an
    already-issued `tenant_owner` token keeps working through the ~30min transition
    window. Used for owner-only actions (onboarding pause switches,
    CONTRACT_onboarding_v1.md §7) that a non-owner should not be able to trigger.

    `secretary` passes as an ALTERNATIVE to `is_owner` (secretary round, 2026-08-14,
    explicit product decision): the receptionist runs the clinic's day-to-day operation,
    including the onboarding pause — today the only route behind this gate. NOTE for
    whoever adds the next owner-only action: it will be open to `secretary` from birth
    because of this line. If that is wrong for your action, gate on `p.is_owner`
    directly instead of reaching for `require_owner`.
    """
    if not (
        p.is_owner
        or p.role == ROLE_SECRETARY
        or p.role == ROLE_TENANT_OWNER  # legacy token transition
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner access required")
    return p


def deny_secretary(p: Principal, error_code: str) -> None:
    """Raise 403 `error_code` when `p` is a `secretary`; a no-op for every other role.

    The `secretary` role is secretarIA-ONLY by product decision (2026-08-14), but the
    role reaches the whole `/doctor/*` router through `require_doctor` — so the exclusion
    only exists where this is called. It is deliberately a plain guard rather than a
    FastAPI dependency: each call site names its own machine-readable `error_code`, and
    `grep -rn deny_secretary` enumerates the complete boundary in one shot.

    The three call sites, all "clinical data or becoming a professional":
      * `api/sso.py`         — `secretary_precheck_not_allowed` (minting a PreCheck session)
      * `api/doctor.py` (x2) — `secretary_precheck_not_allowed` (anamneses, proxied from
        PreCheck; PreCheck's own `BRAIN_DOCTOR_ROLES` would reject the forwarded token
        anyway, but that is a remote 403 surfacing as an opaque upstream error — this
        makes the boundary local, explicit and testable)
      * `api/onboarding.py`  — `secretary_cannot_be_professional` (the self-bind that
        would hand the caller a `professional_id` and put them in the bookable agenda)
    """
    if p.role == ROLE_SECRETARY:
        raise HTTPException(status.HTTP_403_FORBIDDEN, error_code)
