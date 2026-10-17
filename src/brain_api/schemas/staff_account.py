"""Frozen public account contracts. No clinical permission is accepted from a client."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from brain_api.schemas.auth import TokenResponse


class MembershipOut(BaseModel):
    user_id: UUID | None = None
    invite_id: UUID | None = None
    tenant_id: UUID
    clinic_name: str
    status: Literal["active", "pending_invite", "revoked", "pending_first_access"]


class AccountResponse(BaseModel):
    session_kind: Literal["account"] = "account"
    account_token: str
    expires_in: int
    email: str
    name: str
    memberships: list[MembershipOut]
    preferred_user_id: UUID | None
    selection_state: Literal["selection_required", "preferred_active", "preferred_unavailable"]
    clinical_session: TokenResponse | None = None


class SelectMembershipIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    remember: bool = False


class PreferenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID | None = None


class AcceptInviteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str
