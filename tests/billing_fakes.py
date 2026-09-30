"""Shared fakes for the TASK C billing tests (price map, settings, subscription objects).

Not a test module (no `test_` prefix): imported by the TASK C test files. The price map is a
FULL one — direct PreCheck tiers, the three secretarIA metered companions and no anchor price
for secretarIA, the shape production actually has — unlike tests/conftest.py's map, which
gives secretaria_basico a direct price.
"""

import json
from types import SimpleNamespace

PRICE_MAP: dict[str, str] = {
    "complete_clinic_combo": "price_combo",
    "secretaria_basico_metered_patients": "price_sec_pat",
    "secretaria_basico_metered_professionals": "price_sec_pro",
    "secretaria_basico_metered_reminders": "price_sec_rem",
    "precheck_start": "price_pc_start",
    "precheck_basic": "price_pc_basic",
    "precheck_advanced": "price_pc_adv",
    "multi_professional": "price_multipro",
    "pix_deposit": "price_pix",
    "precheck_topup": "price_topup",
}
SEC_PRICES = ("price_sec_pat", "price_sec_pro", "price_sec_rem")


def fake_settings(**overrides) -> SimpleNamespace:
    """A stand-in for `get_settings()` inside services.billing (Stripe configured, TEST key)."""
    base = dict(
        STRIPE_SECRET_KEY="sk_test_fake",
        STRIPE_API_BASE="https://api.stripe.test",
        STRIPE_TIMEOUT_SECONDS=15.0,
        STRIPE_PRICE_MAP=json.dumps(PRICE_MAP),
        STRIPE_CHECKOUT_SUCCESS_URL="http://localhost:3000/app?checkout=success",
        STRIPE_CHECKOUT_CANCEL_URL="http://localhost:3000/app?checkout=cancelled",
        STRIPE_PORTAL_RETURN_URL="http://localhost:3000/app",
        STRIPE_TRIAL_PERIOD_DAYS=0,
        PRECHECK_TOPUP_MIN_QUANTITY=5,
        PRECHECK_TOPUP_MAX_QUANTITY=1000,
        BILLING_ADD_SECRETARIA_ENABLED=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def install_fake_settings(monkeypatch, **overrides) -> SimpleNamespace:
    """Point `services.billing.get_settings` at `fake_settings(**overrides)`."""
    from brain_api.services import billing as billing_service

    settings = fake_settings(**overrides)
    monkeypatch.setattr(billing_service, "get_settings", lambda: settings)
    return settings


def make_sub(
    *items,
    status: str = "active",
    sub_id: str = "sub_1",
    customer: str = "cus_1",
    tenant_id=None,
    period_start: int = 1_700_000_000,
    period_end: int = 1_702_592_000,
    **extra,
) -> dict:
    """A Stripe subscription object as the webhook / `GET /v1/subscriptions/{id}` delivers it.

    `items` are price ids, or `(price_id, quantity)` tuples for a flat/add-on item.
    """
    data = []
    for item in items:
        price_id, quantity = (item, 1) if isinstance(item, str) else item
        data.append({"id": f"si_{price_id}", "price": {"id": price_id}, "quantity": quantity})
    sub: dict = {
        "id": sub_id,
        "object": "subscription",
        "customer": customer,
        "status": status,
        "current_period_start": period_start,
        "current_period_end": period_end,
        "items": {"data": data},
    }
    if tenant_id is not None:
        sub["metadata"] = {"tenant_id": str(tenant_id)}
    sub.update(extra)
    return sub


# --- database helpers for endpoint/webhook tests (same DB the `client` app uses) ---------

_SNAPSHOT_FIELDS = (
    "plan",
    "precheck_plan",
    "status",
    "precheck_enabled",
    "secretaria_enabled",
    "manual_products",
    "addons",
    "limits",
    "stripe_customer_id",
    "stripe_subscription_id",
    "period_start",
    "period_end",
    "charge_hardened_at",
    "cancel_scheduled_at",
)


async def set_entitlement(tenant_id: str, **fields) -> None:
    """Write entitlement columns straight to the DB (no bridges, no webhook): the state a
    courtesy / admin / paid clinic is in before it acts. Creates the row when absent."""
    from uuid import UUID

    from brain_api.models import Entitlement
    from tests.test_courtesy_coupon import _sessao

    async with _sessao() as session:
        tid = UUID(tenant_id)
        ent = await session.get(Entitlement, tid)
        if ent is None:
            ent = Entitlement(tenant_id=tid)
            session.add(ent)
        for name, value in fields.items():
            setattr(ent, name, value)
        await session.commit()


async def snapshot(tenant_id: str) -> dict:
    """Every billing-relevant column + the test window (what a wrong write would change)."""
    from uuid import UUID

    from brain_api.models import Entitlement, Tenant
    from tests.test_courtesy_coupon import _sessao

    async with _sessao() as session:
        tid = UUID(tenant_id)
        ent = await session.get(Entitlement, tid)
        tenant = await session.get(Tenant, tid)
        assert ent is not None and tenant is not None
        snap = {name: getattr(ent, name) for name in _SNAPSHOT_FIELDS}
        snap["test_window_started_at"] = tenant.test_window_started_at
        snap["test_window_notified_at"] = tenant.test_window_notified_at
        return snap


async def set_window(tenant_id: str, *, started_at=None, notified_at=None) -> None:
    from uuid import UUID

    from brain_api.models import Tenant
    from tests.test_courtesy_coupon import _sessao

    async with _sessao() as session:
        tenant = await session.get(Tenant, UUID(tenant_id))
        assert tenant is not None
        tenant.test_window_started_at = started_at
        tenant.test_window_notified_at = notified_at
        await session.commit()


async def read_window(tenant_id: str):
    """(started_at, notified_at) as timezone-aware UTC (SQLite hands back naive values)."""
    from datetime import UTC

    snap = await snapshot(tenant_id)

    def aware(value):
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value

    return aware(snap["test_window_started_at"]), aware(snap["test_window_notified_at"])


class FakeStripe:
    """Records every Stripe call of the add-product flow and answers from canned objects.

    `live`: the `GET /v1/subscriptions/{id}` object; `preview`: the `create_preview` invoice;
    `updated`: the update response; `post_error`: a `StripeApiError` the update raises;
    `on_update`: an async callback run inside the update, BEFORE it answers (to interleave a
    webhook with the endpoint). `calls` is `[(kind, path, data, idempotency_key)]` with kind in
    GET | PREVIEW | UPDATE.
    """

    def __init__(self, *, live=None, preview=None, updated=None, post_error=None, on_update=None):
        self.live = live
        self.preview = preview
        self.updated = updated
        self.post_error = post_error
        self.on_update = on_update
        self.calls: list[tuple[str, str, dict | None, str | None]] = []

    async def get(self, path):
        self.calls.append(("GET", path, None, None))
        return self.live

    async def post(self, path, data, *, idempotency_key=None):
        self.calls.append(("PREVIEW", path, data, idempotency_key))
        return (
            self.preview
            if self.preview is not None
            else make_invoice(
                proration=data.get("subscription_details[proration_behavior]") != "none"
            )
        )

    async def post_or_raise(self, path, data, *, idempotency_key=None):
        self.calls.append(("UPDATE", path, data, idempotency_key))
        if self.on_update is not None:
            await self.on_update()
        if self.post_error is not None:
            raise self.post_error
        return self.updated

    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]

    def call(self, kind: str):
        return next(call for call in self.calls if call[0] == kind)

    def install(self, monkeypatch) -> "FakeStripe":
        from brain_api.services import billing as billing_service

        monkeypatch.setattr(billing_service, "_stripe_get", self.get)
        monkeypatch.setattr(billing_service, "_stripe_post", self.post)
        monkeypatch.setattr(billing_service, "_stripe_post_or_raise", self.post_or_raise)
        return self


async def make_clinic(
    session,
    secretaria_plan=None,
    precheck_plan=None,
    *,
    status="active",
    manual=(),
    subscription="sub_1",
    customer="cus_1",
    **extra,
):
    """A tenant + its entitlement, composed per product family (no HTTP, no bridges)."""
    from brain_api.models import Entitlement, Tenant
    from brain_api.services import catalog

    tenant = Tenant(clinic_name="Clinic")
    session.add(tenant)
    await session.flush()
    state = catalog.compose_entitlement_state(secretaria_plan, precheck_plan)
    ent = Entitlement(
        tenant_id=tenant.id,
        status=status,
        manual_products=list(manual),
        stripe_subscription_id=subscription,
        stripe_customer_id=customer if subscription else None,
        **{**state, **extra},
    )
    session.add(ent)
    await session.commit()
    return ent


def make_invoice(amount=0, *, proration=True, **changes):
    body = {
        "currency": "brl",
        "amount_due": amount,
        "subtotal": amount,
        "total": amount,
        "starting_balance": 0,
        "ending_balance": 0,
        "billing_reason": "subscription_update",
        "lines": {"has_more": False, "data": [{"amount": amount, "proration": proration}]},
    }
    body.update(changes)
    return body
