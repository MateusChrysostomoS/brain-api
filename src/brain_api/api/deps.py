"""Shared FastAPI auth dependencies (auth-jwt-multitenant skill).

The token is validated here into a `Principal` (the stable identity from the JWT).
Mutable/sensitive state (the user row, tenant, entitlements) is looked up server-side
in the services — never trusted from the token.
"""

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from brain_api.core.security import decode_token
from brain_api.models.user import ROLE_TENANT_OWNER, ROLE_TENANT_STAFF


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: UUID | None
    role: str


def get_current_principal(authorization: str | None = Header(default=None)) -> Principal:
    """Turn an `Authorization: Bearer <jwt>` header into a validated Principal."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    claims = decode_token(authorization[7:].strip())
    if claims is None or "sub" not in claims:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    tid = claims.get("tenant_id")
    return Principal(
        user_id=claims["sub"],
        tenant_id=UUID(tid) if tid else None,
        role=claims.get("role", ""),
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


def require_doctor(p: Principal = Depends(get_current_principal)) -> Principal:
    """Require a tenant-scoped doctor user (`tenant_owner` or `tenant_staff`).

    Platform `admin` tokens are rejected with 403 — admins use `/admin/*`, not the doctor
    portal (RBAC task: "/doctor/* routes return 403 for admin tokens, wrong portal"). A
    doctor role always carries a `tenant_id`; its absence is a malformed principal, also
    403. The route then scopes purely by `p.tenant_id` (never a client-supplied id).
    """
    if p.role not in (ROLE_TENANT_OWNER, ROLE_TENANT_STAFF) or p.tenant_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Doctor access required")
    return p
