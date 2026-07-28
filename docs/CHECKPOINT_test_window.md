# CHECKPOINT — Test window (Task 2: reframe the free trial as a Meta/WABA acceptance window)

Status: **BUILT + tested locally (2026-07-22)**, UNPUSHED, migration NOT deployed.
`uv run pytest` → **341 passed** (whole suite, incl. `tests/test_test_window.py`, new).

> See also `docs/CHECKPOINT_onboarding_multiprofessional.md` (the onboarding state
> machine + Phase 1 billing this round builds on top of) and
> `docs/CHECKPOINT_register_at_first_card.md` (`services.signup.provision_tenant_from_intent`,
> touched here). Full commercial/API contract detail for the broader onboarding vertical
> lives in `CONTRACTS.md` — this round is NOT yet folded into that document.

## Why

The Stripe trial (`STRIPE_TRIAL_PERIOD_DAYS`) already behaved as a de-facto acceptance
window for Meta/WABA (WhatsApp Coexistence), but nothing made that framing explicit or
recoverable: a tenant whose Meta review dragged past the trial got auto-cancelled
(`customer.subscription.trial_will_end`) with no local record of *when* their window
started, no way to see progress, and no way to restart it after Meta approval eventually
came through late. This round makes the window a first-class, tenant-visible concept and
moves the billing-hardening anchor earlier — from `ativo` (secretaria fully configured)
to `conectado` (WhatsApp/Meta accepted the connection) — which is the actual moment the
test window exists to protect.

## What changed, and where

### 1. `tenants` gains two columns (migration `0009_test_window`)

- `test_window_started_at` (DateTime tz, nullable) — anchors the window.
- `test_window_notified_at` (DateTime tz, nullable) — one-shot "past-deadline email sent"
  marker.
- `upgrade()` backfills every pre-existing tenant: `test_window_started_at = created_at`
  (same reasoning as `0007_onboarding_state_machine`'s own backfill — no new tenant can
  have been inserted mid-migration, so targeting every NULL row is safe).
- `downgrade()` drops both columns.

### 2. Window start — three triggers, all idempotent-safe

- **Payment completion** (the common case): `services.signup.provision_tenant_from_intent`
  sets `tenant.test_window_started_at = now()` the first time (guarded on
  `is None`) — runs inside the `checkout.session.completed` /
  `metadata.kind == "signup_intent"` webhook path.
- **A genuine subscription-id change**: `services.billing._reset_markers_if_subscription_changed`
  now RETURNS a `bool` (previously `None`) — `True` only when it actually reset
  `charge_hardened_at`/`cancel_scheduled_at` (a real change, not a redelivery of the same
  id). Both call sites in `services.billing.apply_stripe_event`
  (`checkout.session.completed` linking a subscription, and
  `customer.subscription.created`/`.updated`) now call the new
  `services.billing._restart_test_window(session, tenant_id)` helper whenever that
  returns `True` — a resubscription is a fresh test window, same rationale as the
  pre-existing marker reset.
- **A manual restart**: `POST /doctor/onboarding/test-window/restart` (below) always sets
  both fields (`started_at = now()`, `notified_at = None`) in its own commit, regardless
  of which of its two branches ran.

### 3. Harden-at-`conectado` — the core billing-semantics change

`api/onboarding.py`'s `post_attempt` (`POST /doctor/onboarding/attempts`) now calls
`billing.harden_charge(session, tenant)` immediately after a successful `record_attempt`
leaves `tenant.onboarding_state == onboarding.STATE_CONECTADO` — right after that
commit, before the connection-success email / `refresh_config_status` pull. The
PRE-EXISTING `ativo`-time trigger in `services.onboarding_sync.refresh_config_status`
is UNTOUCHED — `harden_charge` is idempotent (`charge_hardened_at` guard), so a tenant
that reaches `conectado` and later `ativo` in the same or a later request is only ever
hardened once.

`harden_charge` itself needed **no changes** — all four guards the contract asked for
were already present: no entitlement row, `charge_hardened_at` already set, `status !=
"trialing"`, no `stripe_subscription_id`. Confirmed by reading it before wiring the new
call site.

Net effect: a tenant accepted by Meta on day 10 of a 30-day window gets `trial_end=now`
on day 10 — the 30-day billing cycle starts there, not whenever `ativo` eventually
happens (which can lag `conectado` by however long secretaria's mode-resolution /
config-completion takes).

### 4. Internal cron contract — `GET /internal/onboarding/tenants`

`InternalOnboardingTenantOut` (`schemas/internal.py`) gains three fields, computed for
every row in `api/internal.py`'s `list_onboarding_tenants`:

- `test_window_email_due: bool` — from the new pure helper
  `services.onboarding_sync.test_window_email_due(tenant, ent, settings, *, now=None)`.
  Due when: `STRIPE_TRIAL_PERIOD_DAYS > 0` AND the window has started AND
  `now >= started_at + days` AND `onboarding_state` is NOT `conectado`/`ativo` AND
  `test_window_notified_at is None` AND an entitlement row exists with a
  `stripe_subscription_id` AND `catalog.get_plan(ent.plan).secretaria` is `True`.
  Deliberately does **not** require `ent.status` to be active/trialing — by the deadline
  the subscription is typically already auto-cancelled
  (`customer.subscription.trial_will_end` already fired `cancel_at`, and Stripe's own
  `customer.subscription.deleted` already flipped `status="canceled"`); that auto-
  cancelled, never-connected, paid population is exactly who this email targets.
- `test_window_days: int` — `settings.STRIPE_TRIAL_PERIOD_DAYS`, so the cron never
  hardcodes it.
- `test_window_restart_url: str` — `f"{settings.FRONTEND_BASE_URL}/app/reativar"`.

### 5. New one-shot cron event: `test_window_email_sent`

`InternalOnboardingEventIn.event` (`schemas/internal.py`) gains the literal
`"test_window_email_sent"`. `services.onboarding_sync.apply_onboarding_event` treats it
as one-shot (`_ONE_SHOT_EVENTS`) and sets `tenant.test_window_notified_at = payload.at`;
`applied=False` no-op if already set. **Naming note**: unlike the other one-shot events,
this one's marker column (`test_window_notified_at`) does NOT follow the
`f"{event}_at"` convention (`test_window_email_sent_at` does not exist) — the exception
is mapped explicitly via `_ONE_SHOT_MARKER_COLUMN`, so the generic one-shot guard still
resolves the right column.

### 6. Doctor-facing endpoints (`api/onboarding.py`, router-level `require_doctor`)

- **`GET /doctor/onboarding/test-window`** → `TestWindowOut`
  (`schemas/onboarding.py`): `applicable`, `days_total`, `started_at`, `deadline_at`,
  `onboarding_state`, `connected_at`, `expired`, `notified`, `subscription_status`,
  `can_restart`. A live read — same plan/day/subscription gates as the cron's
  `test_window_email_due`, minus the deadline/notified requirements (so the portal can
  show progress before AND after the deadline).
- **`POST /doctor/onboarding/test-window/restart`** → `TestWindowRestartOut`
  (`restarted`, `deadline_at`, `payment_method_present`). Three ordered 409 guards:
  `test_window_not_applicable` (days == 0, or plan not secretaria-bearing — this also
  covers "no entitlement row" since `plan` resolves to `None` in that case),
  `already_connected` (state already `conectado`/`ativo`), `checkout_required` (no
  `stripe_customer_id` yet). Two branches, chosen by a LIVE `_stripe_get` on the
  existing subscription (when one exists):
  - **Live, not `canceled`/`incomplete_expired`** → extend it in place:
    `_stripe_post` `{trial_end: <now+days>, proration_behavior: "none", cancel_at: ""}`,
    clear `ent.cancel_scheduled_at`.
  - **Missing, `canceled`, or `incomplete_expired`** → create a brand-new subscription
    (`POST /v1/subscriptions`) against the same customer, `default_payment_method` set
    to the first saved card if any (`GET /v1/payment_methods?customer=…&type=card`,
    fetched once up front so `payment_method_present` is meaningful in BOTH branches —
    the contract only specified fetching it "before creating", but the response shape
    needs the field either way; see Deviations below), and resets the subscription
    markers via the same `_reset_markers_if_subscription_changed`.

  Both branches call every Stripe endpoint BEFORE touching any row, then set
  `test_window_started_at`/`test_window_notified_at` and commit once — a Stripe failure
  never half-commits (matches `harden_charge`'s / `create_checkout_session`'s existing
  transactional shape).

  **Refactor enabling this**: `services.billing._append_checkout_line_items` now
  delegates to a new shared `_selection_price_items(selection) ->
  list[tuple[price_id, quantity | None]]`; a new sibling `_append_subscription_items`
  projects the SAME list into Stripe's `items[i]` form-key shape (Checkout Sessions use
  `line_items[i]`, subscription-create uses `items[i]`) so checkout and the restart
  endpoint share one price-list builder. `_append_checkout_line_items`'s own
  input/output shape is unchanged — every existing caller/test is unaffected.

## Tests

`tests/test_test_window.py` (new), 30 tests: harden-at-`conectado` (incl. a no-op-when-
not-trialing case), the `test_window_email_due` matrix as pure unit tests (11 cases,
including the "due even when `ent.status == canceled`" case and a naive-datetime
comparison case), the internal listing endpoint's wiring (due-logic mocked, only the
three new fields' plumbing is asserted), the `test_window_email_sent` one-shot event,
both restart branches (live-extend and create-new, each asserting the exact captured
Stripe payload), all three restart guards, `provision_tenant_from_intent` setting
`started_at` end-to-end through a real cold-signup + webhook, and both subscription-id-
change cases (genuine change resets the window; same-id redelivery does not).

One pre-existing test helper needed a fix: `tests/test_onboarding_endpoints.py`'s
`_set_pair_key` builds a fake `Settings` stand-in for `api/internal.py`; it was missing
`STRIPE_TRIAL_PERIOD_DAYS`/`FRONTEND_BASE_URL`, which the listing endpoint now always
reads — three pre-existing tests that reach the listing endpoint via this helper started
failing with an `AttributeError` until it got the two new attributes (safe defaults
matching `config.py`'s own: `0` / `"http://localhost:3000"`).

## Deviations from the literal contract

- **`payment_method_present` is computed unconditionally**, not only "before creating".
  The contract's restart-endpoint spec says to fetch payment methods "before creating"
  (i.e., only in the create-new-subscription branch), but the response shape promises
  `payment_method_present` regardless of which branch ran. Fetching it once, up front,
  makes the field meaningful in the live-extend branch too, without fabricating a value.
  `default_payment_method` is still only ever SENT to Stripe in the create branch (an
  existing live subscription doesn't need it re-supplied).
- Nothing else knowingly deviates from the contract as given.

## Known gap (deliberately out of scope, per the contract's own enumeration)

`test_window_started_at` is set by exactly the two triggers the contract named
(payment completion via `provision_tenant_from_intent`, and a subscription-id CHANGE) —
**not** by a tenant's very first `checkout.session.completed` link when that tenant was
never provisioned through the cold-signup flow (e.g. an admin-created tenant whose FIRST
subscription arrives via the authenticated `POST /billing/checkout`). Such a tenant's
`test_window_started_at` stays `NULL` until either a resubscription or the manual
restart endpoint sets it (or, for a tenant that predates migration `0009`, the one-time
backfill already covers it). In practice this only affects non-self-serve (admin/manual)
provisioning, not the self-serve pipeline this feature targets — flagged here rather
than silently patched over, since it affects how `GET /doctor/onboarding/test-window`'s
`applicable=true, started_at=null` combination should be read downstream.

## Pendências

- **Deploy migration `0009_test_window`** (`uv run alembic upgrade head`) — not run
  against any real DB by this round (per instruction; tests use `create_all`, not
  Alembic).
- **EasyPanel `FRONTEND_BASE_URL`** must be set for `test_window_restart_url` to point
  anywhere real (same pre-existing pitfall as the professional-invite link — see
  `docs/CHECKPOINT_onboarding_multiprofessional.md`).
- **secretarIA counterpart** (the `run_onboarding_nudges` cron actually reading
  `test_window_email_due`/`test_window_days`/`test_window_restart_url` and POSTing
  `test_window_email_sent`) is being built in parallel, out of this repo.
- **brain-frontend `/app/reativar`** (the page `test_window_restart_url` points at,
  calling `GET`/`POST /doctor/onboarding/test-window*`) is being built in parallel, out
  of this repo.
- **Next round** (per the handoff): a public intent PATCH endpoint, checkout-config
  add-ons, and relaxing two owner-only invite endpoints — none of that started here.
