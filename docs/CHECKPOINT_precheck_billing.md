# CHECKPOINT — PreCheck billing (plans, quota, top-up, tier upgrade)

> **Superseded in part (2026-09-03):** PreCheck now has **three** tiers (Start 50 /
> Basic 100 / Advanced 300) and real sale prices. The tier ladder, the live Stripe price
> ids, the new `STRIPE_PRICE_MAP` and the **deploy order that must not be inverted** live
> in `CHECKPOINT_tres_faixas_precheck.md`. Everything below still describes the machinery
> correctly — only "two tiers" and the R$1,00 top-up example are outdated.

Status: **BUILT + tested locally (2026-08-01)**, UNCOMMITTED, not deployed.
Full suite (PowerShell, from `C:\TECH\BRAIN\brain-api`):

```
$env:PYTHONPATH = "src;.venv\Lib\site-packages"; & "$env:APPDATA\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe" -m pytest -q
```

→ **405 passed**, 0 failed (whole suite, including `tests/test_precheck_billing.py`, new).
Last run: 2026-08-01, after the avulso per-unit/quantity round described below.

## Why

PreCheck had exactly ONE catalog plan (`"precheck"`) with an EMPTY `base_limits` — sellable
(checkout/webhook/admin PATCH all worked), but nothing metered or capped PreCheck usage,
and there was no way to sell "more consultations" without a plan swap. This round gives
PreCheck two real tiers (Basic/Advanced) with a monthly consultation quota each, a
one-off avulso purchase (priced per pré-consulta) for going over quota without upgrading,
an in-place tier swap, and the
internal API PreCheck itself calls to record a consultation and ask "is this patient still
allowed one".

Per `stripe-billing-entitlements`: PreCheck bills **flat price + quota**, never metered —
a clean second example (alongside `secretaria_basico`'s fully-metered model) of the same
three-concerns separation applied to a different commercial shape.

## What changed, and where

### 1. Catalog (`services/catalog.py`)

- `PLAN_PRECHECK = "precheck"` is no longer a `PLANS` member. Replaced by
  `PLAN_PRECHECK_BASIC = "precheck_basic"` and `PLAN_PRECHECK_ADVANCED =
  "precheck_advanced"` (both `precheck=True, secretaria=False, secretaria_tier=None`).
  `LEGACY_PLAN_ALIASES["precheck"] = PLAN_PRECHECK_BASIC` protects every already-seeded
  row (including the test fixture's Clinic A) — `get_plan("precheck")` now resolves to
  the Basic tier, no data migration needed.
- New limit key `LIMIT_PRECHECK_CONSULTATIONS = "precheck_consultations"`, added to
  `LIMIT_KEYS`. Unlike every secretarIA metering limit (which stays `0` = unenforced on
  every plan), this one carries a REAL per-plan `base_limits` entry — it is the
  enforcement mechanism itself, not just a display number.
- **Deliberate, spec-mandated deviation from "the catalog is all hardcoded"**: the two
  quotas are read from `get_settings()` ONCE, at `services/catalog.py` import time
  (`_PRECHECK_BASIC_QUOTA` / `_PRECHECK_ADVANCED_QUOTA` module-level constants), not
  hardcoded like every other `base_limits` value in this file. Rationale: the PreCheck
  consultation quota is a commercial knob an operator may need to retune (a promo, a
  temporary bump) with an env var change + process restart, no code deploy. Every OTHER
  plan/add-on limit stays a literal on purpose.
- `complete_clinic_combo` (the premium bundle) now also carries the ADVANCED quota in its
  `base_limits` — leaving it at the implicit `0` would have made PreCheck unenforced
  (unlimited) on the most expensive plan, backwards for a combo.
- New module constant `PRECHECK_TOPUP_PRICE_KEY = "precheck_topup"` — a STRIPE_PRICE_MAP
  key, not a plan/add-on id (mirrors `services/billing.py`'s `{plan_id}_metered_*`
  companion-key convention). `services/billing.py::_parse_price_map`'s known-ids set was
  taught to accept it.

### 2. Migration `0010_precheck_billing` (`down_revision = 0009_test_window`)

- New table `precheck_topup_credits` (model: `models/precheck_topup_credit.py`, class
  `PrecheckTopupCredit`): `id` (UUID PK), `tenant_id` (FK `tenants.id` ON DELETE CASCADE,
  indexed), `amount` (int, not null — consultations granted), `amount_total_cents` /
  `currency` (nullable — Stripe's own Checkout Session totals, for the spend summary),
  `stripe_checkout_session_id` (unique, not null — natural idempotency key), `granted_at`
  (server default `now()`), `expires_at` (not null, indexed).
- New composite index `ix_usage_events_tenant_feature_created` on
  `usage_events(tenant_id, feature, created_at)` — backs `services/precheck_billing.py`'s
  usage-window `SUM` query, potentially hit on every consultation attempt via
  `GET /internal/precheck/quota/{tenant_id}`. Mirrored in `models/usage_event.py`'s
  `__table_args__` so the hermetic SQLite test schema (`Base.metadata.create_all`, no
  Alembic in tests) matches production.

### 3. `services/precheck_billing.py` (new)

- `quota_window(ent, now) -> (start, end)`: prefers the tenant's real Stripe billing
  cycle (`ent.period_start`/`period_end`, kept current by the `customer.subscription.*`
  webhook recompute) whenever BOTH are set AND the period has not already ended; else
  falls back to the current UTC calendar month. A `_as_utc` normalizer (mirrors
  `services/signup.py::_as_utc`) guards against SQLite returning naive datetimes for a
  `DateTime(timezone=True)` column.
- `usage_summary(session, ent, now) -> PrecheckUsageSummary`: the ONE resolution both the
  internal quota endpoint and the tenant-facing usage route share. `ent=None` (no
  entitlement row) resolves to a coherent all-zero/not-enforced default instead of
  raising — the two `/internal/precheck/*` routes 404 BEFORE calling this; the
  tenant-facing route wants exactly this default (200 always).
  - `enforced` = resolved plan exists AND `plan.precheck` AND the effective
    `LIMIT_PRECHECK_CONSULTATIONS` limit > 0 — limits are resolved the SAME way
    `services.entitlements.resolve_entitlement` does (plan base + addon grants, with an
    admin's manual `limits` override merged on top and winning).
  - `used` = `SUM(usage_events.amount)` for `tenant + feature=precheck_consultations`
    inside `[window_start, window_end)`.
  - `topup_credits` = `SUM(precheck_topup_credits.amount)` where `expires_at > now`
    (balance — NOT window-scoped, a credit can outlive the window it happened to be
    queried from as long as it hasn't hit ITS OWN expiry).
  - `spend` = `SUM(amount_total_cents)` + `COUNT(*)` + `currency` of credits GRANTED
    inside the current window (`granted_at` scoped — a reporting figure, distinct from
    the balance).
  - `remaining = max(0, quota + topup_credits - used)`; `allowed = (not enforced) or
    remaining > 0`.
- Recording `precheck_consultations` through `services/usage.py::record_usage` does
  **not** fire a Stripe meter event — no code change was needed: that module's
  `_METER_EVENT_SETTINGS` dict simply has no entry for this feature, so the meter-forward
  branch is skipped by construction. Verified by a test that monkeypatches
  `_forward_meter_event` to raise if ever called.

### 4. Internal PreCheck API (`api/internal_precheck.py`, `schemas/internal_precheck.py`, new)

A **separate service identity** from the existing secretarIA-facing `/internal/*`
surface (`api/internal.py`) — PreCheck gets its OWN shared secret pair, never
secretarIA's, per `auth-jwt-multitenant`'s "each mesh caller gets its own credential"
rule.

- `require_precheck_api_key`: `X-Internal-Api-Key` checked via `secrets.compare_digest`
  against `PRECHECK_API_KEY` (or `_PREVIOUS`, rotation window). Fails CLOSED — 403 when
  the server key is unset, 401 on mismatch — mirroring `api/internal.py::
  require_internal_api_key` exactly.
- `POST /internal/precheck/usage-events` `{tenant_id, event_id, amount=1 (1..100)}` → 200
  `{recorded}`. 404 `entitlement_not_found` if the tenant has no entitlement row at all
  (unlike the secretarIA-facing usage-events route, which upserts one — PreCheck usage
  always rides an already-provisioned plan). Recording is **unconditional**: an
  over-quota consultation still records — that IS the signal the quota endpoint reports.
  Internally builds a `schemas.internal.UsageEventIn(feature=LIMIT_PRECHECK_CONSULTATIONS,
  ...)` and calls the existing `services.usage.record_usage`.
- `GET /internal/precheck/quota/{tenant_id}` → 200
  `{enforced, allowed, quota, used, topup_credits, remaining}`. 404 same as above. No
  message field — PreCheck composes its own patient-facing text.

### 5. Billing routes (`api/billing.py` + `services/billing.py`)

- **`POST /billing/precheck/topup`** (`require_tenant`) body `{quantity}` → `{url}`. The
  **first `mode=payment`** Checkout Session in this codebase (every other session here is
  `mode=subscription` or, for cold signup, `mode=setup`). The top-up Price is **per unit**
  (one pré-consulta), so the doctor names the quantity in the BRAIN UI and Stripe bills
  `quantity × unit price` — there is no fixed pack size, and the session is created
  **without `adjustable_quantity`**, so Stripe's hosted page never re-asks for it.
  Checks in order: 409 `not_precheck_plan` unless the tenant's resolved plan is
  PreCheck-enabled; 422 `quantity_below_minimum` / `quantity_above_maximum` against
  `PRECHECK_TOPUP_MIN_QUANTITY` / `PRECHECK_TOPUP_MAX_QUANTITY` (re-checked HERE, not just
  in the schema — the frontend's own minimum is convenience, never the enforcement point,
  and the bounds are operator-tunable env values so they cannot be `Field` constraints).
  Line item = the `PRECHECK_TOPUP_PRICE_KEY` price at that quantity; `customer` set when
  the tenant already has a `stripe_customer_id`; `metadata = {tenant_id, kind:
  "precheck_topup", quantity: str(quantity)}` — the quantity is stamped AT PURCHASE TIME
  so a later bounds change can never alter a purchase someone already paid for.
- **Webhook**: `apply_stripe_event`'s `checkout.session.completed` handling now branches
  on `metadata.kind == "precheck_topup"` BEFORE the generic customer/subscription
  link-ids fallback (that fallback does not apply to a one-off payment session anyway).
  `_apply_precheck_topup_checkout` inserts a `PrecheckTopupCredit` row (`amount` from
  `metadata.quantity`, so the grant is always exactly what Stripe charged for;
  `expires_at = quota_window(ent, now)[1]` — a top-up never
  outlives the window it was bought in). Idempotency: `processed_stripe_events` (same
  transaction as every other event) is the PRIMARY guard; a **belt-and-braces** second
  guard catches the narrower case of a genuinely different `event.id` completing the SAME
  Checkout Session (e.g. a Stripe dashboard resend) — the insert runs inside a SAVEPOINT
  (`session.begin_nested()`), and the unique constraint on `stripe_checkout_session_id`
  turns a duplicate into a caught `IntegrityError` / no-op rather than crashing the whole
  webhook apply.
- **`POST /billing/precheck/upgrade`** (`require_tenant`) body `{plan}` → the same shape
  as `GET /billing/precheck/usage` (below). Swaps between the two PreCheck tiers via a
  LIVE Stripe subscription-item price swap. Checks, in order: 422
  `invalid_precheck_plan:{id}` (target not one of the two PreCheck plans); 409
  `not_precheck_plan` (current resolved plan isn't PreCheck-enabled); 409
  `already_on_plan` (target == current, canonical ids); 409 `no_active_subscription` (no
  `stripe_subscription_id`); 503 `price_not_configured:{id}` (either plan's Stripe price
  missing); 409 `subscription_price_mismatch` (defensive — the live subscription carries
  no item at the current plan's price at all). Retrieves the subscription
  (`GET /v1/subscriptions/{id}`), finds the item at the CURRENT plan's price, then
  `POST`s `items[0][id]` / `items[0][price]=<target price>` /
  `proration_behavior="create_prorations"` — the ONE deliberate exception to this
  module's usual "proration none" convention (a mid-cycle tier swap should true up the
  difference). Then OPTIMISTICALLY updates the local entitlement (`plan` + recompute via
  `catalog.compute_entitlement_state`, mirroring `services.admin.update_entitlement`'s
  own plan-change semantics — a bare plan swap resets addons/limits to the new plan's
  defaults, no addon overrides carried over) so the new quota applies immediately; the
  later `customer.subscription.updated` webhook recompute confirms independently.
- **`GET /billing/precheck/usage`** (`require_tenant`) → 200 ALWAYS:
  `{plan, plan_name, precheck_enabled, enforced, quota, used, remaining, topup_credits,
  topup_expires_at, window_start, window_end, spend: {topup_cents, topup_count,
  currency}}`. Zeros/false when the tenant's plan isn't PreCheck-enabled (no entitlement
  row included) — the frontend hides the section then, never treats it as an error.
  `topup_expires_at` is the current window's end whenever `topup_credits > 0` (every
  credit is granted expiring at ITS OWN window's end, so the current window's end is the
  right "use them or lose them" date), else `null`.
- **Small fix**: the cold-signup webhook's `onboarding_sync.ensure_secretaria_provisioned`
  call (`services/billing.py`, `checkout.session.completed` / `signup_intent` branch) is
  now gated on the just-activated entitlement's `secretaria_enabled` flag — a
  PreCheck-only signup must not ping secretaria provisioning. Deliberately narrower than
  `api/onboarding.py`'s OWN (still-ungated) lazy-retry call to the same bridge: that call
  site has existing tests relying on the ungated behavior for a not-yet-purchased tenant
  manually reaching the wizard (see `docs/CHECKPOINT_register_at_first_card.md`'s
  "Deliberately NOT changed" section) — untouched here. Covered by two new tests in
  `tests/test_signup.py` (precheck-only signup never calls the bridge; a
  secretaria-enabling signup still does).

### 6. New settings (`config.py`, `.env.example`)

| Setting | Default | Purpose |
|---|---|---|
| `PRECHECK_START_CONSULTATIONS_PER_MONTH` | `50` | Start (entry) tier's monthly quota — added 2026-09-03 |
| `PRECHECK_BASIC_CONSULTATIONS_PER_MONTH` | `100` | Basic tier's monthly quota (catalog-import-time read) |
| `PRECHECK_ADVANCED_CONSULTATIONS_PER_MONTH` | `300` | Advanced tier's (and the combo's) monthly quota |
| `PRECHECK_TOPUP_MIN_QUANTITY` | `5` | Smallest avulso purchase (422 `quantity_below_minimum` under it) |
| `PRECHECK_TOPUP_MAX_QUANTITY` | `1000` | Largest avulso purchase — typo/abuse guard, not a business rule |
| `PRECHECK_API_KEY` | `""` | PreCheck's OWN `/internal/precheck/*` shared secret; empty fails closed (403) |
| `PRECHECK_API_KEY_PREVIOUS` | `""` | Rotation window only (verification only — `docs/key-rotation.md`) |

`STRIPE_PRICE_MAP` gains three new recognized keys: `precheck_basic` / `precheck_advanced`
(recurring, flat monthly) and `precheck_topup` (one-off, **per unit**). The legacy bare
`precheck` key is still accepted (normalizes to `precheck_basic` via `LEGACY_PLAN_ALIASES`).

## Stripe Dashboard steps (deploy)

1. Create **one recurring Price** (monthly) per PreCheck tier — as of 2026-09-03 that is
   **three**: "PreCheck Start", "PreCheck Basic" and "PreCheck Advanced". The live ones
   are already created; their ids are in `CHECKPOINT_tres_faixas_precheck.md` §1.
2. Create **one one-off Price** for the avulso pré-consulta: **Standard pricing**
   (deliberately NOT "Package pricing" — package sells in fixed blocks, which is exactly
   the model this replaced), **R$1,00 BRL**, recurrence **One time**. The quantity is
   supplied per purchase by brain-api, so this Price only ever describes ONE
   pré-consulta.
3. In EasyPanel, add all three price ids to `STRIPE_PRICE_MAP` under the keys
   `precheck_basic`, `precheck_advanced`, `precheck_topup` (see `.env.example` for the
   full JSON shape alongside the existing secretarIA/add-on keys).
4. Set `PRECHECK_API_KEY` to a fresh random secret
   (`python -c "import secrets; print(secrets.token_hex(32))"`) on brain-api, and
   configure PreCheck with the SAME value (byte-for-byte) as its outbound
   `X-Internal-Api-Key` to `brain-api`'s `/internal/precheck/*` routes.
5. Optionally tune `PRECHECK_BASIC_CONSULTATIONS_PER_MONTH` /
   `PRECHECK_ADVANCED_CONSULTATIONS_PER_MONTH` away from their code defaults (100 / 300)
   if the launch numbers differ — takes effect on the next process restart (these are read
   once, at `services/catalog.py` import time). `PRECHECK_TOPUP_MIN_QUANTITY` /
   `PRECHECK_TOPUP_MAX_QUANTITY` (5 / 1000) are read per request, but the frontend
   mirrors the minimum as a constant (`PrecheckBillingSection`'s `TOPUP_MIN_QUANTITY`),
   so lowering it server-side alone would not make the UI offer less.

## Migration

`0010_precheck_billing` (`down_revision = 0009_test_window`) — run `alembic upgrade
head` at deploy. Purely additive (one new table + two new indexes); no backfill, no
existing-row rewrite.

## Decisions taken

- **Quota window = the Stripe billing cycle, with a calendar-month fallback.** A tenant
  with a real subscription (the common case) gets its quota reset on ITS OWN billing
  anniversary, not the 1st of the month — consistent with how Stripe already invoices
  it. The calendar-month fallback exists for a tenant with no Stripe period on record yet
  (mid-signup, an admin-materialized row), so the quota check is never undefined.
- **Top-up credits expire at the window's end**, never carried over — a tenant cannot
  stockpile credits indefinitely; each purchase is scoped to the period it was bought to
  cover.
- **Avulso is priced PER pré-consulta, with a minimum of 5 per purchase** (superseding the
  original fixed 50-consultation pack). The doctor picks the quantity in the BRAIN UI, so
  Stripe's hosted page is never asked to adjust it; the minimum keeps a purchase worth
  processing, and both bounds are enforced server-side whatever the frontend sends.
- **Enforcement rule**: `enforced` (whether the quota gate applies at all) is a pure
  function of the CATALOG plan (`plan.precheck` + a nonzero configured limit) — it
  deliberately does NOT also require `ent.status in (active, trialing)` the way the
  general `is_entitled`/`check_quota` gates do. This keeps the internal quota decision
  simple and catalog-driven; PreCheck's own product judges whether a lapsed subscription
  should still answer patient messages at all (a status check) separately from "does this
  plan have a quota" (this check).
- **Usage is always recorded, even over quota.** `POST /internal/precheck/usage-events`
  never itself refuses — the over-quota signal comes back from the NEXT
  `GET /internal/precheck/quota/{tenant_id}` call. This mirrors the rest of the codebase's
  metering convention (`services/usage.py::record_usage` has no allow/deny concept
  either) and keeps the ledger a complete, honest record of what actually happened.

## Tests

`tests/test_precheck_billing.py` (new, 32 tests): `quota_window` (Stripe-period vs
calendar-month fallback, both pure/no-DB); `usage_summary` (ent=None default, unenforced
non-precheck plan, usage-in-window vs outside, credit balance vs expiry, spend-in-window,
remaining clamped at 0); the two internal endpoints (auth fail-closed 403/401, 404 unknown
tenant, idempotent recording + never-meters, allowed/blocked/unenforced quota decisions);
top-up checkout (409 non-precheck; mode=payment + per-unit line-item quantity + metadata +
no `adjustable_quantity`, via the existing fake-Stripe-httpx helper; 422 below-minimum and
above-maximum, both asserted to never reach Stripe at all; the minimum itself accepted as a
boundary); the webhook top-up grant (credit granted at EXACTLY the purchased quantity,
event-id replay no-op, DIFFERENT-event-id-same-session-id no-op via the SAVEPOINT guard); upgrade (all four 409/422 paths,
the price-swap-and-proration payload + local entitlement update via monkeypatched
`_stripe_get`/`_stripe_post`, the price-mismatch defensive 409); `GET /billing/precheck/
usage`'s shape for both a PreCheck tenant and a non-PreCheck one, plus its tenant-scope
gate. `tests/test_signup.py` gained the `ensure_secretaria_provisioned` gating regression
pair described above. `tests/test_catalog_entitlements.py` and `tests/test_billing.py`
were extended with the alias/env-default/combo-quota and price-map-key coverage the
catalog restructuring needed.

Two pre-existing tests needed updating because of the catalog restructuring (not new
bugs — direct, foreseeable consequences of splitting one `PLANS` entry into two and
zero-filling every limit key including the new one):
- `tests/test_catalog_entitlements.py::test_assignable_plan_ids_has_no_reserved_slots`
  and `::test_get_entitlements_owner_a_precheck_plan` — asserted an exact
  `ASSIGNABLE_PLAN_IDS` set / an all-zero limits dict that both changed shape once
  `precheck` stopped being a `PLANS` member and `LIMIT_PRECHECK_CONSULTATIONS` started
  carrying a real quota.
- `tests/test_billing.py::test_price_map_accepts_secretaria_ferro_alias` — its
  `"precheck"` price-map key now ALSO normalizes through the alias (to `precheck_basic`),
  since `LEGACY_PLAN_ALIASES` is shared by `catalog.get_plan` and
  `services.billing._parse_price_map`.
- `tests/test_signup.py::test_patch_intent_plan_change_not_allowed` — was targeting the
  bare `"precheck"` id as the "different plan" in a plan-change-rejection test; that id is
  no longer a `PLAN_IDS` member on its own (only a `LEGACY_PLAN_ALIASES` key), so the
  schema-level catalog-id check now fires first (422) instead of the intended 409 —
  switched the test to the canonical `"precheck_basic"` id, which exercises the same
  `plan_change_not_allowed` path the test is actually about.

No pre-existing failures on the untouched baseline — the two failures seen mid-session
were in this round's OWN new test file (a test-data bug: fixed day offsets like
`now - timedelta(days=2)` can land in the previous calendar month whenever `now` is early
in the month, which it is today; fixed by anchoring offsets to the resolved window's own
start instead of to `now` directly).

## At deploy

1. `alembic upgrade head` (migration `0010_precheck_billing`).
2. Stripe Dashboard steps above (2 recurring Prices + 1 one-off PER-UNIT Price at R$1,00,
   Standard pricing).
3. EasyPanel env: add the 3 new `STRIPE_PRICE_MAP` keys, set `PRECHECK_API_KEY` (share it
   with PreCheck out of band — never commit it), review the quota defaults and the avulso
   min/max bounds.
4. Nothing else — no data backfill, no other service's deploy is a prerequisite. PreCheck
   itself needs to start calling `POST /internal/precheck/usage-events` and
   `GET /internal/precheck/quota/{tenant_id}` with the shared key (out of scope for this
   repo).
