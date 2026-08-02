"""Pydantic v2 schemas for brain-api's INBOUND `/internal/precheck/*` surface
(PreCheck-facing).

A SEPARATE service identity from `schemas/internal.py`'s secretarIA-facing surface
(gated by `PRECHECK_API_KEY`, not `SECRETARIA_API_KEY` — see `api/internal_precheck.py`).
PreCheck never names a feature id: it is always `catalog.LIMIT_PRECHECK_CONSULTATIONS`,
fixed server-side by the endpoint, never client-supplied.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PrecheckUsageEventIn(BaseModel):
    """`POST /internal/precheck/usage-events` body — one completed PreCheck consultation.

    `event_id` is PreCheck's own idempotency key (`services/usage.py` dedupes on it, same
    contract as the secretarIA-facing `UsageEventIn`). Recording is UNCONDITIONAL: an
    over-quota consultation still records — that is precisely the signal
    `GET /internal/precheck/quota/{tenant_id}` reports back — this endpoint never itself
    decides allow/deny.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    event_id: str = Field(min_length=1, max_length=128)
    amount: int = Field(default=1, ge=1, le=100)


class PrecheckUsageEventOut(BaseModel):
    """`recorded=False` means a duplicate `event_id` was replayed — not an error."""

    recorded: bool


class PrecheckQuotaOut(BaseModel):
    """`GET /internal/precheck/quota/{tenant_id}` response — the allow/deny DECISION plus
    the numbers behind it. No message field: PreCheck composes its own patient-facing
    text from `allowed`/`remaining`.
    """

    enforced: bool
    allowed: bool
    quota: int
    used: int
    topup_credits: int
    remaining: int
