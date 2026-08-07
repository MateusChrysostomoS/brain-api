"""Shared FastAPI auth dependencies (auth-jwt-multitenant skill).

The token is validated here into a `Principal` (the stable identity from the JWT).
Mutable/sensitive state (the user row, tenant, entitlements) is looked up server-side
in the services — never trusted from the token.
"""

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from brain_api.core.security import decode_token
from brain_api.models.user import ROLE_DOCTOR, ROLE_MANAGER, ROLE_TENANT_OWNER, ROLE_TENANT_STAFF

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
DOCTOR_ROLES = (ROLE_DOCTOR, ROLE_MANAGER)


def require_doctor(p: Principal = Depends(get_current_principal)) -> Principal:
    """Require a tenant-scoped doctor-portal user (`doctor` or `manager`; LEGACY:
    `tenant_owner`/`tenant_staff` on a not-yet-expired pre-taxonomy token).

    Platform `admin` tokens are rejected with 403 — admins use `/admin/*`, not the doctor
    portal (RBAC task: "/doctor/* routes return 403 for admin tokens, wrong portal"). A
    doctor role always carries a `tenant_id`; its absence is a malformed principal, also
    403. The route then scopes purely by `p.tenant_id` (never a client-supplied id).
    """
    if p.role not in (*DOCTOR_ROLES, *_LEGACY_DOCTOR_ROLES) or p.tenant_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Doctor access required")
    return p


def require_owner(p: Principal = Depends(require_doctor)) -> Principal:
    """Require the doctor-scoped principal to be the tenant OWNER.

    Layers on top of `require_doctor` (still 403 for admin / tenant-less tokens). Owner is
    now `p.is_owner` (role-taxonomy round) rather than a role string; a LEGACY token
    (pre-taxonomy, no `is_owner` claim) falls back to its old role check so an
    already-issued `tenant_owner` token keeps working through the ~30min transition
    window. Used for owner-only actions (onboarding pause switches,
    CONTRACT_onboarding_v1.md §7) that a non-owner should not be able to trigger.
    """
    if not (p.is_owner or p.role == ROLE_TENANT_OWNER):  # legacy token transition
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner access required")
    return p
