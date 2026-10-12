"""Swagger contract for one-clinic patient cleanup; no patient data in responses."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool = False
    clinic_name: str = Field(
        min_length=1, max_length=255, description="Exact clinic name shown by preview."
    )


class ProductResult(BaseModel):
    status: Literal["ready", "completed", "not_provisioned", "blocked", "failed"]
    counts: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CleanupResult(BaseModel):
    tenant_id: UUID
    clinic_name: str
    status: Literal["ready", "blocked", "completed", "partial"]
    products: dict[str, ProductResult]
    warnings: list[str] = Field(
        default_factory=lambda: [
            "appointments_payments_and_external_reports_are_preserved",
            "google_calendar_and_asaas_are_not_modified",
            "run_when_clinic_intake_is_idle_counts_can_change_after_preview",
        ]
    )
