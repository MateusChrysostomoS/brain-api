# CHECKPOINT — Register at the first card (cold-signup login/entitlement split)

Status: **BUILT + tested locally (2026-07-21)**, UNPUSHED, not deployed. No migration.

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
