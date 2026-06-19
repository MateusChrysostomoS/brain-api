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


class EntitlementOut(BaseModel):
    """`GET /entitlements` payload — resolved entitlement state for one tenant.

    `addons` / `limits` / `usage` are JSON scaffolds (empty `{}` for the MVP). No
    secrets and no plan flags ever live in the JWT; this is the source of truth the
    frontend `getEntitlements()` consumes.
    """

    model_config = ConfigDict(extra="ignore")

    tenant_id: UUID
    clinic_name: str
    products: ProductsOut
    plan: str
    status: str
    addons: dict = Field(default_factory=dict)
    limits: dict = Field(default_factory=dict)
    usage: dict = Field(default_factory=dict)
