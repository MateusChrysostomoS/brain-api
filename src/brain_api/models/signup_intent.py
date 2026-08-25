"""SignupIntent model — cold-signup lead through Stripe Checkout to entitlement activation.

The bridge state machine for "stranger fills a signup form -> pays on Stripe -> gets an
active paid tenant without any human touching it" (services/signup.py). The tenant + owner
user + inert entitlement are created up front by `register_signup` (this row is linked to
that tenant via `tenant_id` from the moment it is created); the intent then tracks the
PAYMENT lifecycle. One row per attempt:

- pending_payment: registered; a Checkout Session may or may not have been opened yet.
- completed: `checkout.session.completed` (metadata.kind == "signup_intent") resolved
  back to this row and `services.signup.provision_tenant_from_intent` ACTIVATED the
  already-existing tenant's inert entitlement to the purchased plan.
- failed: activation could not proceed (e.g. the linked tenant no longer exists) —
  `failure_reason` records why. The webhook STILL acks and marks the Stripe event
  processed; nothing here ever blocks Stripe.

The onboarding token is minted by `GET /public/onboarding-status` (never the webhook)
because only that synchronous poll can hand the PLAINTEXT back to the browser — only its
SHA-256 hash is ever stored here (same scheme as `RefreshToken.token_hash`). Each poll
while `onboarding_token_used_at` is NULL rotates it (overwrites hash + expiry); redeeming
it (`POST /auth/exchange-onboarding-token`) sets `onboarding_token_used_at` and nulls the
hash, burning it for good.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class SignupIntent(Base):
    """One cold-signup attempt: lead data + catalog selection through to provisioning."""

    __tablename__ = "signup_intents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    name: Mapped[str] = mapped_column(String(255))
    clinic_name: Mapped[str] = mapped_column(String(255))
    # Stored lower-cased (mirrors users.email); reused as-is for the provisioned User row.
    email: Mapped[str] = mapped_column(String(320))
    whatsapp_phone: Mapped[str] = mapped_column(String(32))
    # The validated catalog ids the buyer selected: exactly one plan + N add-ons
    # (schemas/signup.py enforces this at request time).
    catalog_ids: Mapped[list] = mapped_column(JSON, server_default=text("'[]'"), default=list)
    # Optional pre-checkout intake answers (CONTRACT_onboarding_v1.md §7; schemas/signup.
    # py IntakeIn — whatsapp_usage/prior_api/fb_page). None when omitted (back-compat).
    # Consumed once, at provisioning, by services.onboarding.provision_defaults to derive
    # the tenant's initial onboarding_state/blocker_reason/next_retry_at.
    intake: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: Slug do template de especialidade do PreCheck escolhido na vitrine (/comecar).
    #: NULL em intents anteriores a 0015 e em compras sem PreCheck; o bridge cai em
    #: "clinica-geral" nesse caso.
    precheck_template_slug: Mapped[str | None] = mapped_column(String(100), nullable=True)

    #: Código do cupom de cortesia resgatado, quando a clínica foi ativada por
    #: cupom em vez de pagamento. É a trilha de auditoria: sem ele, um tenant
    #: ativo e sem `stripe_subscription_id` seria indistinguível de uma falha de
    #: cobrança que ativou por engano.
    courtesy_coupon_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # pending_payment | completed | failed
    status: Mapped[str] = mapped_column(
        String(32), server_default="pending_payment", default="pending_payment"
    )
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The latest Checkout Session created for this intent (a retry overwrites it).
    stripe_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Set by provisioning once the tenant is materialized.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True
    )

    # Opaque onboarding token: only its SHA-256 hash is stored (core/security.
    # hash_refresh_token — the same primitive RefreshToken uses). Rotated on every
    # onboarding-status poll until exchanged once.
    onboarding_token_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    onboarding_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    onboarding_token_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
