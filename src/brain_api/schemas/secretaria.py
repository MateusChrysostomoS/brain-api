"""Request schema for the brain-api -> secretaria admin proxy (CONTRACTS.md §11).

Kept separate from `schemas/admin.py` (which models brain's OWN admin resources) because
this describes a call into a DIFFERENT service. The response is returned verbatim from
secretaria, so there is no `*Out` model here.
"""

from pydantic import BaseModel, ConfigDict, Field


class SecretariaResetIn(BaseModel):
    """Body for `POST /admin/secretaria/reset` — a DESTRUCTIVE data wipe on secretaria.

    `confirm` must be true (defense against an accidental curl/fat-finger); `include_tenants`
    also truncates secretaria's tenants table when true (its next inbound webhook re-provisions
    a tenant from environment settings).
    """

    model_config = ConfigDict(extra="forbid")

    confirm: bool = Field(..., description="Must be true to proceed with the wipe.")
    include_tenants: bool = Field(
        False, description="Also truncate secretaria's tenants table."
    )
