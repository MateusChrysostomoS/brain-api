"""Pydantic v2 schemas for the LGPD privacy orchestration (CONTRACTS.md §14).

Both `POST /admin/privacy/erasure` and `POST /admin/privacy/export` share the SAME
request/response shape — one identifies a subject and either wipes or collects
everything the mesh (brain + secretaria + precheck) knows about them. Reachable ONLY by
a platform `admin` JWT (router-level `require_role("admin")`; see `api/privacy.py`).
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class PrivacyRequestIn(BaseModel):
    """Identify the subject of an erasure/export and confirm the (possibly destructive)
    action. `confirm` must be `true` (checked in the route, mirroring the secretaria
    reset guard — CONTRACTS.md §11.2) — a missing/false confirm is `400` before any
    local or upstream work happens.

    `subject_type="user"` (a brain portal user, e.g. a tenant owner) requires `email`;
    `subject_type="patient"` (a WhatsApp/PreCheck patient, who has no brain account)
    requires `wa_id` + `tenant_id`. Either subject MAY also carry the other identifier
    (e.g. a patient with a known email) — it only widens what gets erased/exported.
    """

    model_config = ConfigDict(extra="forbid")

    subject_type: Literal["patient", "user"]
    email: EmailStr | None = Field(default=None, max_length=320)
    wa_id: str | None = Field(default=None, max_length=32)
    tenant_id: UUID | None = None
    confirm: bool = Field(..., description="Must be true to proceed.")

    @model_validator(mode="after")
    def _check_subject_combo(self) -> "PrivacyRequestIn":
        if self.subject_type == "user" and not self.email:
            raise ValueError("subject_type 'user' requires 'email'")
        if self.subject_type == "patient" and (not self.wa_id or self.tenant_id is None):
            raise ValueError("subject_type 'patient' requires 'wa_id' and 'tenant_id'")
        return self


class PrivacyResultOut(BaseModel):
    """Response for both the erasure and export endpoints.

    `brain`/`secretaria`/`precheck` are each either that service's own payload (counts
    for erasure, the collected bundle for export) or `{"status": "failed" |
    "skipped_unconfigured" | "not_applicable"}` — never raised as an HTTP error, so one
    failing/unconfigured leg never blocks the others (CONTRACTS.md §14). `status` is
    `"completed"` only if every applicable leg succeeded, else `"partial"`.
    """

    status: Literal["completed", "partial"]
    brain: dict[str, Any]
    secretaria: dict[str, Any]
    precheck: dict[str, Any]
    request_id: UUID
