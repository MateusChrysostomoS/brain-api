"""Who sees the whole clinic agenda in secretarIA's hub (owner decision 2026-10-09 A).

secretarIA ENFORCES the rule (its hub routes filter and refuse); brain-api, the identity
authority, DECIDES it - at every hub-token introspection, from the acting user's CURRENT
row (auth-jwt-multitenant: mutable role state is read at request time, never trusted
from a token). The hub token itself is unchanged: it names the acting user in `act`.

    "clinic" - sees every professional's appointments: the clinic's owner/manager
               ("gestor": role `manager`, or a `doctor` with `is_owner`/`is_manager`,
               or the LEGACY `tenant_owner` during its token window) and the
               receptionist (`secretary`, who runs every agenda by product decision).
    "own"    - only the appointments of the user's own professional: any other doctor,
               and every case this module cannot prove (unknown/malformed actor, a user
               of another clinic, a role it does not know). Fail closed.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.models import User
from brain_api.models.user import (
    ROLE_DOCTOR,
    ROLE_MANAGER,
    ROLE_SECRETARY,
    ROLE_TENANT_OWNER,
)

AGENDA_SCOPE_CLINIC = "clinic"
AGENDA_SCOPE_OWN = "own"

_CLINIC_WIDE_ROLES = (ROLE_SECRETARY, ROLE_MANAGER, ROLE_TENANT_OWNER)


def agenda_scope_for(role: str, *, is_owner: bool, is_manager: bool) -> str:
    """The rule, pure. Roles are the exact stored strings (models/user.py)."""
    if role in _CLINIC_WIDE_ROLES:
        return AGENDA_SCOPE_CLINIC
    if role == ROLE_DOCTOR and (is_owner or is_manager):
        return AGENDA_SCOPE_CLINIC
    return AGENDA_SCOPE_OWN


async def resolve_agenda_scope(session: AsyncSession, tenant_id: UUID, actor: object) -> str:
    """The scope of the hub session whose token names `actor` (the `act` claim) and
    acts for `tenant_id` (the `sub` claim). Never raises for a bad `actor`."""
    try:
        user_id = UUID(str(actor))
    except (ValueError, TypeError, AttributeError):
        return AGENDA_SCOPE_OWN
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != tenant_id:
        return AGENDA_SCOPE_OWN
    return agenda_scope_for(
        user.role or "", is_owner=bool(user.is_owner), is_manager=bool(user.is_manager)
    )
