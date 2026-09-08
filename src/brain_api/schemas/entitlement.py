"""Pydantic v2 schemas for the entitlements vertical (CONTRACTS.md §3.1).

The resolved entitlement state the portal reads after login to decide which products
to show/link and what plan/limits apply. Per the stripe-billing-entitlements skill this
is built from the LOCAL `entitlements` row — there is NO Stripe call to answer the read.

This is a hand-built response: the service constructs it explicitly (not via
`from_attributes`), because `products` is a derived/renamed view of the
`precheck_enabled` / `secretaria_enabled` columns rather than a 1:1 attribute map.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProductsOut(BaseModel):
    """Per-product access flags (CONTRACTS.md §3.1 `products`).

    `precheck` <- entitlements.precheck_enabled, `secretaria` <- entitlements.secretaria_enabled.
    """

    precheck: bool
    secretaria: bool


class ChannelsOut(BaseModel):
    """Per-channel delivery flags (CONTRACTS.md §3.1 `channels`).

    `whatsapp` <- tenants.whatsapp_enabled, `brain_message` <- tenants.brain_message_enabled.

    Deliberately shaped like `ProductsOut` (a set of independent bools, not an exclusive
    enum) and deliberately sourced from `tenants`, not `entitlements`/`catalog`: a channel
    is HOW the clinic talks to patients, an operational property of the tenant, while a
    plan is WHAT it bought. Both can be on during a gradual migration off WhatsApp.
    """

    whatsapp: bool
    brain_message: bool


class EntitlementOut(BaseModel):
    """`GET /entitlements` payload — resolved entitlement state for one tenant.

    `channels` is the tenant's delivery channels (`tenants.whatsapp_enabled` /
    `brain_message_enabled`) — additive, and the reason it lives here rather than in a
    second call is that the portal already reads this payload to decide what to render.

    `addons` / `limits` carry the FULL formalized keysets from `services/catalog.py`
    (every add-on id -> bool; every limit key -> int), normalized through the catalog so
    even a pre-catalog row reads as a complete shape. `secretaria_tier` is derived from
    the plan (additive field; the frontend's four consumed fields are unchanged). No
    secrets and no plan flags ever live in the JWT; this is the source of truth the
    frontend `getEntitlements()` consumes.
    """

    model_config = ConfigDict(extra="ignore")

    tenant_id: UUID
    clinic_name: str
    products: ProductsOut
    channels: ChannelsOut
    plan: str
    secretaria_tier: str | None = None
    status: str
    addons: dict = Field(default_factory=dict)
    limits: dict = Field(default_factory=dict)
    usage: dict = Field(default_factory=dict)
