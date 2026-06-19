"""Pydantic v2 schemas for the demo-request vertical (CONTRACTS.md §4 + §5).

Public lead capture for the "Agendar demo" form. The request model validates the
optional enum fields (`profile` / `product_interest` / `source`) at the edge and stores
them as plain strings on the model. It also carries a HONEYPOT field (`website`) used
purely for anti-spam (CONTRACTS.md §5): it is NEVER persisted — the API layer inspects
it and silently accept-and-drops a request that fills it.

This endpoint is isolated lead capture: no tenant, no entitlement, no Stripe, no async
work (CONTRACTS.md §0.4).
"""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class ProfileEnum(StrEnum):
    """Who the lead is (CONTRACTS.md §4 `profile`). secretarIA-variant radio maps here."""

    clinica_privada = "clinica_privada"
    medico_autonomo = "medico_autonomo"
    secretaria_municipal = "secretaria_municipal"
    hospital = "hospital"
    outro = "outro"


class ProductInterestEnum(StrEnum):
    """Which product the lead wants (CONTRACTS.md §4 `product_interest`).

    brain-variant radio maps here (PreCheck -> precheck, secretarIA -> secretaria,
    Os dois -> ambos).
    """

    precheck = "precheck"
    secretaria = "secretaria"
    ambos = "ambos"


class SourceEnum(StrEnum):
    """Which surface the lead came from (CONTRACTS.md §4 `source`). Defaults to brain."""

    brain = "brain"
    secretaria = "secretaria"
    precheck = "precheck"


class DemoRequestCreate(BaseModel):
    """Body for `POST /demo-requests` (CONTRACTS.md §4.1).

    Both enum fields are optional server-side: the single `ContactForm` radio group means
    one of `profile` / `product_interest` is always sent `null` depending on the variant.
    """

    name: str = Field(min_length=1, max_length=255)
    email: EmailStr = Field(max_length=320)
    clinic: str | None = Field(default=None, max_length=255)
    profile: ProfileEnum | None = None
    product_interest: ProductInterestEnum | None = None
    message: str | None = Field(default=None, max_length=2000)
    # Optional client hint; the service defaults this to "brain" when None.
    source: SourceEnum | None = None
    # HONEYPOT (anti-spam, CONTRACTS.md §5). A real browser leaves this hidden field
    # empty; a bot fills it. It is NEVER persisted — the API layer accept-and-drops when
    # it is non-empty.
    website: str | None = None

    @field_validator("name")
    @classmethod
    def _trim_name(cls, v: str) -> str:
        """Trim `name` and reject blank-after-trim (422)."""
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class DemoRequestConfirmation(BaseModel):
    """`POST /demo-requests` response (CONTRACTS.md §4.1). No lead data echoed back."""

    id: UUID
    status: str
    message: str
