# CHECKPOINT — Register at the first card (cold-signup login/entitlement split)

Status: **BUILT + tested locally (2026-07-21)**, UNPUSHED, not deployed. No migration.

> See `docs/CHECKPOINT_test_window.md` (2026-07-22, Task 2): `provision_tenant_from_intent`
> (below) now also stamps `tenant.test_window_started_at` at the same payment-completion
> moment it activates the entitlement. See also the **"Update 2026-07-22 — corrections
> round"** section at the bottom of THIS file: a new `PATCH /public/signup-intents/{id}`
> add-on-update route + `GET /public/checkout-config`'s new `addons` field, both additive
> on top of the split below.

## Why

The old cold-signup flow only created the `Tenant` + owner `User` + `Entitlement` inside
the Stripe webhook (`provision_tenant_from_intent`), and the owner's password was a random
`secrets.token_urlsafe(32)` that was never communicated. Net effect the founder observed:
a visitor pays, the clinic is created correctly, but there is **no moment where they set a
real password** — they can use the dashboard right after paying (session in the browser)
but can **never log back in from another device / after logout**. Separately, abandoning
the wizard before the final step captured **no lead at all**, even though name / clinic /
email / WhatsApp were typed on the very first card.

## The split (the architectural change)

"Can log in" is now separated from "is entitled to a paid product":

- **`services.signup.register_signup`** — the SOLE writer of `Tenant` + owner `User`
  (role `tenant_owner`, **real password hash** from the password chosen on the first card)
  + **inert** `Entitlement` (`status="inactive"`, `plan="free"`, both products off) +
  `SignupIntent` linked to the tenant. Runs at `POST /public/signup-intents` (the first
  card) and returns a real session (`TokenResponse`, access+refresh). 409 on a duplicate
  email (up-front check + `users.email` unique constraint). This replaces the old
  "webhook is the sole tenant/user/entitlement writer" invariant.
- **`services.signup.provision_tenant_from_intent`** — the SOLE entitlement-ACTIVATION
  writer. The tenant/user/inert-entitlement already exist; it looks the tenant up via
  `intent.tenant_id`, flips the entitlement to the purchased plan (from
  `intent.catalog_ids` via `catalog.compute_entitlement_state`), links the Stripe ids, and
  seeds onboarding state from `intent.intake`. **Idempotent on `intent.status == "completed"`**
  (no longer on `tenant_id`, which registration always sets). Missing tenant ⇒
  `status="failed"`, `failure_reason="tenant_missing"` (no raise; the webhook still acks).

Login access is gated by the entitlement the visitor actually paid for — a registered but
unpaid tenant resolves cleanly (`resolve_entitlement`) to "nothing purchased yet" (both
products false, status inactive), so `/entitlements` → the `/app` NoEntitlementsPanel; the
webhook then activates secretaria-only / precheck-only / combo per `catalog_ids`.

## Files changed (backend, `brain-api`)

| file | change |
|---|---|
| `schemas/signup.py` | `SignupIntentCreate` gains required `password` (8-72, ≥1 letter + ≥1 digit — same policy as `SetPasswordIn`); `SignupIntentOut` → `SignupRegisterOut {intent_id, session: TokenResponse}` |
| `services/signup.py` | `create_signup_intent` → `register_signup` (creates tenant+user+inert-ent+linked intent, returns `Registration{intent, user}`); new `attach_intake`; `provision_tenant_from_intent` rewritten to ACTIVATE an existing tenant's entitlement (idempotent on status); module docstring rewritten |
| `api/public_signup.py` | `POST /public/signup-intents` now registers + returns a session (reuses `build_session_response` + `issue_refresh_token`); honeypot returns an empty session; docstring rewritten |
| `api/auth.py` | `_session_pair` → `build_session_response` (public, reused by registration) |
| `api/onboarding.py` | new authenticated `POST /doctor/onboarding/intake` → `signup.attach_intake` |
| `services/billing.py` | `_apply_signup_intent_checkout` now keys "newly provisioned" on the intent's status transition to `completed` (was `tenant_id is not None`, always true now) so the secretaria bridge still fires exactly once; docstrings updated |
| `models/signup_intent.py`, `schemas/auth.py` (`SetPasswordIn`) | docstrings corrected for the new split |
| `tests/test_signup.py` | rewritten for the new shape (registration returns a session, login-before-payment, registered-but-not-entitled, webhook activates an inert entitlement + idempotent replay, intake endpoint, `tenant_missing` fail path). Back-compat `_create_intent = _register` alias kept for `tests/test_billing_phase1.py` |

**No Alembic migration**: `signup_intents.tenant_id` was already nullable, and the inert
entitlement uses existing `entitlements` column defaults.

## Tests

`uv run pytest` — **312 passed** (whole suite). The signup vertical: 28 passed.

## Deliberately NOT changed / open considerations

- **`onboarding_sync.ensure_secretaria_provisioned` is NOT gated on entitlement.** Existing
  tests (`test_get_onboarding_shape_default_state`, etc.) seed tenants with
  `secretaria_enabled=false` yet expect the lazy provisioning bridge to run on
  `GET /doctor/onboarding` — gating it would break them. Consequence: a registered-but-unpaid
  owner who MANUALLY navigates to `/secretaria/configuracao` (the wizard never routes them
  there before payment) would trigger a premature secretaria provisioning. Bounded (creates
  a secretaria tenant row; no charge, no WhatsApp) and pre-existing in spirit (precheck-only
  signups already provision secretaria). **Recommendation if this proves undesirable:** gate
  the bridge on `secretaria_enabled` AND update those onboarding tests together.
- Unpaid registered tenants now appear in `GET /internal/onboarding/tenants` with
  `subscription_active=false`. secretarIA's `run_onboarding_nudges` cron already receives
  that flag and is expected to skip inactive tenants — **verify on the secretarIA side**
  that it does (that repo's code was not in scope here).

## Pending (to ship)

- Push brain-api + brain-frontend; deploy; no migration to run.
- Manual browser click-through of `/cadastro` (see the frontend checkpoint).

## Update 2026-07-22 — corrections round: intent PATCH + checkout-config add-ons

Validated 2026-07-22 (`uv run pytest` → whole-suite green, see
`docs/CHECKPOINT_test_window.md` for the count this round started from). Two additive,
backend-only changes on top of the split above — no migration, nothing here touches the
writer invariants documented above (register/activate stay exactly as described).

- **`PATCH /public/signup-intents/{intent_id}`** (new route, `api/public_signup.py::
  update_signup_intent_catalog` → `services.signup.update_intent_catalog`) — the
  frontend's pre-checkout add-on picker calls this right before
  `POST /public/checkout-sessions` to replace `catalog_ids` on a still-`pending_payment`
  intent. Shares the SAME `_limiter` bucket as the other three `/public/*` signup routes
  (mirrors `_check_rate_limit(request)`). Request `schemas.signup.
  SignupIntentCatalogPatchIn {catalog_ids: list[str]}` re-runs the EXACT registration
  validation (`_validate_catalog_ids`, factored out of `SignupIntentCreate` into a shared
  module-level function both schemas call): known ids only, exactly one assignable
  non-free plan. Response `SignupIntentCatalogOut {intent_id, catalog_ids, status}`.
  - `404 signup_intent_not_found` — unknown id.
  - `409 intent_not_pending` — `intent.status != "pending_payment"` (already paid or
    failed; nothing left to configure).
  - `409 plan_change_not_allowed` — the new selection's derived plan differs from the
    one already stored. **Add-ons are the only mutable part** — a plan swap is a
    different commercial decision (would need re-validating eligibility/intake), so it
    is rejected outright rather than silently applied.
  - `409 addon_not_available` — a requested add-on has no Stripe price in
    `billing.price_id_for` for THIS environment. Defense-in-depth: the frontend is only
    supposed to ever offer add-ons `GET /public/checkout-config`'s new `addons` field
    (below) reports as `available`.
  - On success, `services.signup._normalize_catalog_ids` persists a stable order (plan
    id first, add-ons deduped in the order given) regardless of what order/duplication
    the client sent.
- **`GET /public/checkout-config`** gains `addons: [{id, available}]` — one entry per
  `catalog.ADDON_IDS` id, stable alphabetical order, `available` = that id has a
  configured Stripe price (`billing.price_id_for`, same lookup the PATCH route's
  `addon_not_available` guard uses). `trial_period_days` is unchanged. Still deliberately
  outside the shared rate limiter and DB-free (catalog + settings only) — unchanged from
  the 2026-07-19 addition (see the multi-professional checkpoint's own update).
- **Files touched**: `schemas/signup.py` (shared `_validate_catalog_ids`; new
  `SignupIntentCatalogPatchIn`/`SignupIntentCatalogOut`/`AddonAvailabilityOut`;
  `CheckoutConfigOut.addons`), `services/signup.py` (new `update_intent_catalog` +
  `_normalize_catalog_ids`), `api/public_signup.py` (new PATCH route; `checkout_config`
  builds the `addons` list; module docstring/imports updated). `tests/test_signup.py`
  gained the PATCH coverage (success add/remove, normalization, 404, both 409s, 422,
  rate-limit sharing) plus a dedicated checkout-config addons-availability test.
- **Deliberately not done**: no admin/authenticated equivalent of this PATCH (the
  authenticated post-purchase upsell path already has its own mechanism — see
  `services.billing.create_checkout_session` / the doctor billing routes — this endpoint
  is pre-checkout only, scoped to a `pending_payment` intent).
