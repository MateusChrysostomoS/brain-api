"""Clinic team responses whitelist identity and status, never credentials."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

TeamRole = Literal["doctor", "manager", "secretary"]
DeliveryStatus = Literal["not_requested", "queued", "sent", "failed"]


class TeamInviteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    role: TeamRole
    is_manager: bool = False
    specialty: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def valid_profile(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Name is required")
        if self.role == "doctor" and len(self.name) > 24:
            raise ValueError("Professional name must fit the channel's 24-character list title")
        if self.role == "secretary" and self.is_manager:
            raise ValueError("A secretary cannot receive management privileges")
        if self.role == "manager":
            self.is_manager = True
        return self


class TeamMember(BaseModel):
    user_id: UUID
    name: str
    email: str
    role: TeamRole
    is_manager: bool
    is_owner: bool
    professional_id: UUID | None
    access_status: Literal["pending_first_access", "active", "revoked"]
    delivery_status: DeliveryStatus


class TeamInviteResult(BaseModel):
    member: TeamMember
    delivery_status: DeliveryStatus


class TeamManagementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_manager: bool
