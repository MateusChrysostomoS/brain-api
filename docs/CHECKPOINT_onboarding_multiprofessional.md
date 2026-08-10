# CHECKPOINT — Onboarding state machine, multi-professional & billing Phase 1

> See also `docs/cross-db-erasure.md` and `docs/key-rotation.md` for the LGPD/key-rotation
> rounds this one builds on top of. Full API/data contract detail now lives in
> `CONTRACTS.md` §6.6/6.3e, §13.3/§13.4, §15, §16 — this file is the "what landed where and
> what's still pending" record. See `docs/CHECKPOINT_test_window.md` (2026-07-22, Task 2)
> for the later round that moves `harden_charge`'s trigger earlier (to `conectado`,
> alongside the pre-existing `ativo` trigger) and adds the tenant-visible test-window
> concept on top of `tenants.onboarding_state`/`entitlements` as tracked here. See the
> **"Update 2026-07-22 — corrections round"** section further down THIS file for the
> same-day relaxation of `/doctor/professionals/invites`+`/self` from owner-only to any
> doctor (pause stays owner-only). See `docs/CHECKPOINT_coexistence.md` (2026-08-09) for
> the later "WhatsApp Coexistence onboarding" round that adds a second Meta-side
> `subscribed_apps` webhook-subscription gate to `POST /doctor/onboarding/attempts`.

Validated 2026-07-18 (`uv run python -m pytest -q` → **289 passed**, 3 pre-existing
deprecation warnings unrelated to this round, ~3m18s). This is the record of the
onboarding-eligibility state machine (`pending → aquecimento → aguardando_elegibilidade` /
`aguardando_acao_manual → conectado → ativo`), multi-professional linkage, and Phase 1
(metered + trial) billing round, built against `CONTRACT_onboarding_v1.md` (frozen
cross-service contract, scratchpad copy).

## Retroactive find: the cold-signup vertical (migration `0006`) was already real, just undocumented

Before touching this round's own migration, verification turned up that
**`0006_signup_intents`** (2026-07-16) — the self-serve "stranger fills a form → pays on
Stripe → gets a tenant with no human in the loop" pipeline — was already fully built and
tested, but had **zero** coverage in `CONTRACTS.md`. It is now written up properly as
CONTRACTS.md §15 (endpoints: `POST /public/signup-intents`, `POST
/public/checkout-sessions`, `GET /public/onboarding-status`, `POST
/auth/exchange-onboarding-token`, `POST /auth/set-password`; table `signup_intents`, §6.6).
This round's own contribution to that vertical is small and additive: `signup_intents.intake`
(migration `0007`) and `services.onboarding.provision_defaults` seeding the new tenant's
onboarding state right after `services.signup.provision_tenant_from_intent` creates it.

## Migration `0007_onboarding_state_machine` — what it actually adds

- `tenants` += 14 columns: `onboarding_state` (String(40), server_default `'pending'`),
  `blocker_reason` (String(40), nullable), `config_status` (String(40), server_default
  `'incompleta'`), 9 nullable `DateTime(timezone=True)` timestamps
  (`onboarding_anchor_at`, `next_retry_at`, `connected_at`, `activated_at`,
  `secretaria_provisioned_at`, `config_reminder_anchor_at`, `last_config_reminder_at`,
  `closing_email_sent_at`, `manual_review_flagged_at`), and 2 booleans (`retry_paused`,
  `config_reminder_paused`, both server_default `false`). **Backfill confirmed in the
  migration body**: `UPDATE tenants SET onboarding_state = 'ativo'` and `SET config_status
  = 'completa'` unconditionally on `upgrade()` — every pre-existing tenant is grandfathered
  in as already-onboarded (the reasoning comment in the migration is explicit: no new
  tenant can have been inserted mid-migration, so targeting the whole table is safe).
- NEW table `signup_attempts` — client-supplied UUID PK (the id IS the idempotency key, no
  server-generated id), `tenant_id` FK CASCADE indexed, `source`/`result`/`blocker_reason`/
  `error_code`/`day_offset`/`created_at`. Matches the contract exactly.
- `users` += `professional_id` (UUID, nullable, **no FK** — cross-service value reference
  to `secretaria.professionals.id`, same convention as `tenant_id`-style refs elsewhere),
  `invite_token_hash` (String(64), nullable, indexed), `invite_token_expires_at`.
- `entitlements` += `charge_hardened_at` (DateTime tz, nullable).
- `signup_intents` += `intake` (JSON, nullable) — the only touch to the pre-existing table.

## Modules — what landed where

- **`services/onboarding.py`** — pure state-machine logic, no I/O (deliberate scope
  discipline stated in its own module docstring: no HTTP, no secretaria client, no
  Stripe). Confirmed functions match the contract 1:1: `derive_initial_state`,
  `initial_next_retry_at`, `map_error_to_blocker`, `record_attempt` (idempotent on
  `attempt_id`, the sole writer of `signup_attempts` rows + the transition it drives),
  `resolve_blocker`, `apply_config_status`, `provision_defaults`. All enum constants
  (`STATE_*`, `BLOCKER_*`, `CONFIG_STATUS_*`, `ATTEMPT_RESULT_*`, `ATTEMPT_SOURCE_*`) live
  here as the single source of truth; `models/tenant.py` imports them rather than
  hardcoding strings.
- **`services/onboarding_sync.py`** — the I/O orchestration layer on top: 
  `ensure_secretaria_provisioned` (best-effort `POST /internal/tenants` bridge, no-op once
  `secretaria_provisioned_at` is set, called post-signup-webhook AND lazily on every `GET
  /doctor/onboarding`), `refresh_config_status` (TTL-throttled per-tenant, in-process,
  per-process cache keyed by `CONFIG_STATUS_PULL_TTL_SECONDS` — pulls secretaria's
  config-status, applies `apply_config_status`, and on
  `conectado`+`completa`+mode-resolved-or-fallback-due calls secretaria `/activate`,
  flipping to `ativo` and invoking `services.billing.harden_charge`), plus the internal
  cron-facing helpers `list_onboarding_tenants` and `apply_onboarding_event` (the latter
  distinguishes one-shot events — `closing_email_sent`, `manual_review_flagged`, no-op if
  already set — from recurring ones — `retry_nudge_sent`, `config_reminder_sent`, always
  applied).
- **`services/secretaria_provisioning.py`** — the WRITE-side sibling of the existing
  `services/secretaria_internal.py` READ client. **Confirmed same shared secret**:
  `X-Internal-Api-Key` = `SECRETARIA_API_KEY` (same pair-key invariant as CONTRACTS.md
  §12.1 — must equal secretaria's own `INTERNAL_API_KEY` byte-for-byte). Every function
  (`provision_tenant`, `connect_whatsapp`, `get_config_status`, `create_professional`,
  `activate_tenant`, `send_notification_email`) returns `None`/a sentinel/`False` instead
  of raising — never raises itself, unlike `secretaria_client.py`/`precheck_handoff.py`;
  the module docstring is explicit that the same operations are reused in both fail-soft
  contexts (the provisioning bridge, config-status pull, activation, notification emails)
  and fail-loud ones (the invite/self-bind routes map a `None` from `create_professional`
  to a clean `502`, since those are foreground writes the doctor is waiting on).
- **`services/meta_graph.py`** — `exchange_code_for_token(code)`: `GET
  {META_GRAPH_BASE_URL}/oauth/access_token`, never raises, returns `None` on any failure
  (unconfigured `META_APP_ID`/`META_APP_SECRET`, unreachable, non-200, missing
  `access_token`). `META_APP_SECRET` sent only as a query param on this one call, never
  logged.
- **`api/onboarding.py`** — `GET /doctor/onboarding`, `POST /doctor/onboarding/attempts`,
  `POST /doctor/onboarding/resolve-blocker`, `POST /doctor/onboarding/pause` (owner-only),
  `GET /doctor/professionals`, `POST /doctor/professionals/invites`, `POST
  /doctor/professionals/self`. All under router-level `require_doctor`; pause additionally
  requires `require_tenant_owner`. **As of the 2026-07-22 corrections round (see the
  update section below), the two professional-creation routes NO LONGER require
  `require_tenant_owner`** — they were owner-only at the time this bullet was first
  written (2026-07-18) but are now open to any doctor (owner or staff); only pause stays
  owner-only. Every route matches the contract's request/response shapes exactly
  (verified against `schemas/onboarding.py`).
- **`api/internal.py`** additions — `GET /internal/onboarding/tenants` (batched
  owner+entitlement lookups, avoids N+1, mirrors `services/admin.py::list_tenants`'s
  style) and `POST /internal/onboarding/tenants/{tenant_id}/events`, both on the existing
  pair-key-gated internal router alongside §12's other internal routes.

## JWT / auth changes (confirmed in `core/security.py`, `api/deps.py`, `api/auth.py`)

- `create_access_token(..., professional_id: str | None = None)` and
  `create_hub_token(..., professional_id: str | None = None)` both add the claim only
  when truthy. `Principal.professional_id: UUID | None` parses it defensively (a bad UUID
  in the claim silently resolves to `None` rather than a 500).
- `GET /auth/me` exposes `professional_id` and `name` on the user block (confirmed at
  `api/auth.py` — `_session_pair`/me builder passes `professional_id=user.professional_id`,
  `name=user.name`).
- `POST /auth/exchange-invite-token` mirrors the pre-existing (now-documented, see above)
  `POST /auth/exchange-onboarding-token` almost exactly — same `TokenResponse` shape, same
  single-use hashed-token scheme (`hash_refresh_token`, burned on redeem), same
  `429` rate-limit budget. Redemption calls `services/auth.py::exchange_invite_token`,
  which nulls `invite_token_hash`/`invite_token_expires_at` in the same commit. The
  invited user is expected to follow up with the also-undocumented-until-now `POST
  /auth/set-password` (pre-existing, shared with the cold-signup owner flow).

## Billing Phase 1 (confirmed in `services/billing.py`, `services/usage.py`, `services/catalog.py`)

- `catalog.py`: `LIMIT_BILLABLE_PATIENTS = "billable_patients"` added to `LIMIT_KEYS`
  (limits stay 0/unlimited-by-quota — exists for metering/ledger validation only, per the
  module's own comment).
- `services/billing.py`: `METERED_SUFFIX = "_metered"` — a `{plan_id}_metered`
  `STRIPE_PRICE_MAP` key is recognized (not a catalog id of its own) by both
  `_parse_price_map` validation and `_append_checkout_line_items`, which appends it as an
  extra line item with **no** `quantity` field (confirmed — Stripe rejects quantity on a
  metered price). `_apply_trial` adds `subscription_data[trial_period_days]` when
  `STRIPE_TRIAL_PERIOD_DAYS > 0` (default 0/off). **Both** checkout builders
  (`services.billing.create_checkout_session` AND
  `services.signup.create_checkout_session_for_intent`, i.e. the authenticated upsell path
  and the cold-signup path) call the same two shared helpers — confirmed no duplicated/
  drifted logic between them.
- `harden_charge(session, tenant)`: only acts when the entitlement has a
  `stripe_subscription_id` AND `status == "trialing"`; posts `trial_end=now`,
  `proration_behavior=none`; sets `charge_hardened_at` (idempotent — skips once set).
  Called from `onboarding_sync.refresh_config_status` right after a tenant flips to
  `ativo`.
- `services/usage.py::_forward_meter_event`: fires **after** the local `usage_events`
  ledger + `entitlements.usage[feature]` commit already succeeded, only when
  `STRIPE_METER_EVENT_BILLABLE_PATIENTS` is set AND the entitlement has a
  `stripe_customer_id`; POSTs `/v1/billing/meter_events`; never raises into the caller.
  This is the TODO the pre-existing module docstring flagged — now implemented.

## Update 2026-07-19 — fully-metered `secretaria_ferro` pricing + trial-expiry cancellation

Validated 2026-07-19 (`uv run python -m pytest -q` → **311 passed**, same 3 pre-existing
deprecation warnings — the count includes a same-day adversarial-review hardening pass,
see the dedicated bullet below). Business decision (upstream of this repo):
`secretaria_ferro` moves to a FULLY METERED model — **R$80/month per active professional
+ R$2/month per billable patient** (previously documented above as "the fixed R$80 +
metered R$3 split" — that framing is now superseded; there is no fixed/anchor price on
the plan at all). Full detail in `CONTRACTS.md` §13.3/§13.5/§13.6.

- **Price-map convention: two companions, no anchor.** `services/billing.py`'s single
  `METERED_SUFFIX = "_metered"` companion convention described above is now the
  SUPERSEDED shape. Two companion keys replace it: `{plan_id}_metered_patients` /
  `{plan_id}_metered_professionals` (`METERED_PATIENTS_SUFFIX` /
  `METERED_PROFESSIONALS_SUFFIX`), each an independent optional Checkout line item (no
  `quantity` field). The legacy single `_metered` key is still ACCEPTED by
  `_parse_price_map` (recognized-but-unused graceful degradation for a not-yet-migrated
  deployed map) but no code path reads it anymore. `validate_selection` now WAIVES the
  plan's own `price_id_for(plan.id)` requirement whenever **BOTH**
  `{plan.id}_metered_patients` AND `{plan.id}_metered_professionals` are configured — a
  fully-metered plan has no direct price to require. (Adversarial-review fix: an
  EARLIER version of this waiver required only the professionals companion, which would
  let a half-configured map check out a subscription silently missing the patients
  meter — that usage dimension would accrue in the local ledger but never actually get
  invoiced. Now BOTH are required, or the plan falls back to needing a direct price.)
- **Integration fix: `_state_from_subscription` companion-as-plan-evidence.** With no
  anchor price, a fully-metered subscription's Stripe `items` are ONLY the two companion
  prices. Before this fix, the recompute would find no plan price, return `None`, and
  the entitlement would never get `plan`/products/`limits` set (logging
  `stripe_subscription_no_known_plan` forever). `_plan_id_from_metered_companion` now
  recognizes a companion price id as evidence of its stripped plan id when that plan id
  is a real catalog plan; a companion item never enters `addon_qty` (it isn't an
  add-on).
- **`active_professionals` metering.** New `catalog.LIMIT_ACTIVE_PROFESSIONALS =
  "active_professionals"` (metering-only, same pattern as `LIMIT_BILLABLE_PATIENTS` —
  distinct from the pre-existing `LIMIT_PROFESSIONALS` QUOTA key) + new
  `STRIPE_METER_EVENT_ACTIVE_PROFESSIONALS` setting. `services/usage.py` generalized
  `_forward_meter_event` to take an `event_name` parameter and dispatches feature →
  event-name via `_METER_EVENT_SETTINGS`, so both `billable_patients` and
  `active_professionals` share the identical fire/never-raise/fail-soft mechanics.
  secretarIA's onboarding crons (sibling change, out of this repo) are the caller that
  emits `feature="active_professionals"`.
- **Trial-expiry cancellation — the "nothing cancels a never-approved tenant" gap is
  now BUILT.** Previously, a tenant whose trial ran out without ever reaching `ativo`
  (Meta never approved WhatsApp Coexistence) would simply get charged at trial end —
  nothing canceled it. `apply_stripe_event` now handles
  `customer.subscription.trial_will_end` (full 9-step design in `CONTRACTS.md` §13.6):
  a cheap event-payload short-circuit, THEN a row-locked re-read (`with_for_update`,
  shared with `harden_charge`) of the local guards (still trialing, not hardened, not
  already scheduled), THEN a plan-scope check, THEN a LIVE `GET
  /v1/subscriptions/{id}` verify, and only then a native `POST /v1/subscriptions/{id}
  {cancel_at: <LIVE trial_end>, proration_behavior: "none"}`, stamping the new
  `entitlements.cancel_scheduled_at` column (migration `0008_billing_meter_pricing`).
  Deliberately NO fail-soft anywhere in this branch (unlike `harden_charge`) — a Stripe
  failure propagates so the webhook 500s and Stripe redelivers on its own ~3-day
  schedule, since this handler has no other retry trigger. `harden_charge`'s Stripe
  payload now also carries `cancel_at: ""` (unconditionally clearing any scheduled
  cancellation) and resets `cancel_scheduled_at` to `None` in the same commit — so a
  tenant that activates AFTER a cancellation was scheduled doesn't get canceled anyway.
  When an unactivated subscription's `cancel_at` actually fires, Stripe's existing
  `customer.subscription.deleted` handling (status=canceled, both product flags off)
  performs the entitlement revocation — no new direct-cancellation call was added
  anywhere.
- **Adversarial-review hardening pass (same day), four real defects found and fixed:**
  1. The original design trusted the EVENT's embedded subscription snapshot
     (`obj.get("status")`) as its sole race guard — an unverified assumption about
     Stripe's payload. Replaced with a row lock (`with_for_update`, shared with
     `harden_charge`, so the two critical sections serialize against each other) PLUS a
     live `GET /v1/subscriptions/{id}` (`_stripe_get`, new — mirrors `_stripe_post`)
     that is now the actual authority: schedules only when the LIVE status is
     "trialing" and the LIVE `trial_end` is more than an hour out, and cancels using the
     LIVE `trial_end`, never the event's. This also closes the hole where
     `harden_charge`'s Stripe call succeeds but its OWN DB commit then fails.
  2. **Critical, empirically confirmed:** `charge_hardened_at`/`cancel_scheduled_at`
     survived subscription replacement, permanently disabling both `harden_charge` and
     `trial_will_end` scheduling for a tenant's SECOND subscription (cancel →
     resubscribe → the new trial ran completely unprotected). Fixed with
     `_reset_markers_if_subscription_changed`, called wherever a subscription id gets
     linked (`checkout.session.completed` and `customer.subscription.created`/
     `.updated`): resets both markers to `None` when the incoming subscription id is
     non-null and differs from the one already stored. A redelivery of the SAME id
     changes nothing.
  3. The fully-metered waiver (bullet above) originally required only the
     professionals companion — fixed to require BOTH, closing a silent
     under-billing hole (see that bullet).
  4. The handler had no plan scoping at all — it would have auto-cancelled a
     happily-paying PreCheck-only subscription at trial end (PreCheck has no
     WhatsApp/Coexistence component and structurally can never reach `ativo`). Fixed
     with a `catalog.get_plan(ent.plan).secretaria` gate: only secretarIA-bearing plans
     are ever scheduled for cancellation; an unresolved plan also falls through to
     normal billing (fail toward charging, never toward killing a subscription).
- **`STRIPE_TRIAL_PERIOD_DAYS` no longer sits at its 0/off code default in the deployed
  environment** — per this round's business decision the deployed value is **75** days
  (the code default itself is unchanged at `0`; nothing in code hardcodes 75).
- **New public endpoint**: `GET /public/checkout-config` returns
  `{trial_period_days: <STRIPE_TRIAL_PERIOD_DAYS>}` — non-secret, no DB touch,
  deliberately NOT part of the shared `/public/*` signup rate limiter (a
  pricing-page view must never eat the per-IP signup budget). Consumed by a sibling
  brain-frontend change for pre-checkout disclosure copy. (2026-07-22 corrections round,
  below: this response gains an `addons` field — still non-secret, no DB touch.)

## Update 2026-07-22 — corrections round: staff-level invites/self, public add-on picker

Backend pieces of a corrections round; full detail (request/response shapes, error
codes) for the public-signup half lives in `docs/CHECKPOINT_register_at_first_card.md`'s
own "Update 2026-07-22" section — this bullet covers only the RBAC relaxation that
belongs to this checkpoint's `api/onboarding.py` surface.

- **`POST /doctor/professionals/invites` and `POST /doctor/professionals/self` are no
  longer owner-only.** Both routes moved from `Depends(require_tenant_owner)` to
  `Depends(require_doctor)` — a `tenant_staff` token now gets a normal 2xx/4xx domain
  response instead of `403`. Rationale: day-to-day professional management (inviting a
  colleague, binding yourself to the calendar) belongs on the same "configuracao"
  surface a staff member already operates, unlike the onboarding kill-switches.
  `POST /doctor/onboarding/pause` is UNCHANGED and deliberately stays owner-only
  (`require_tenant_owner`) — it is not part of that day-to-day surface.
- Docstrings/comments that said "(owner only)" for these two routes were corrected in
  `api/onboarding.py` (module docstring, router-level comment, route summaries) and
  `schemas/onboarding.py` (`ProfessionalInviteIn`/`ProfessionalSelfIn`); `PauseIn`'s
  "(owner only)" is accurate and was left as-is.
- **Tests** (`tests/test_onboarding_endpoints.py`): `test_invite_professional_requires_
  owner_role` / `test_self_bind_requires_owner_role` (previously asserting `403` for a
  staff token) were replaced with `test_invite_professional_allows_staff` /
  `test_self_bind_allows_staff` (asserting the staff token now reaches the normal
  success path, `201`/`200`). `test_pause_requires_owner_role` is UNCHANGED — pause must
  still `403` for staff, and does.
- No migration; no other RBAC surface touched (verified by re-running the full
  `tests/test_rbac.py` + `tests/test_onboarding_endpoints.py` green — see
  `docs/CHECKPOINT_test_window.md` for the suite count this round started from).

## Deviations from `CONTRACT_onboarding_v1.md` found while verifying

- **`map_error_to_blocker` substring map is richer than the frozen contract text.** The
  contract (§8) only specifies one substring rule: `'previously'`/`'in use by another'` →
  `numero_em_outro_bsp`, with everything else falling to the declared/`'outro'` default.
  The shipped implementation (`services/onboarding.py::map_error_to_blocker`) does that
  (as three independent hints: `"previously"`, `"another"`, `"in use"` — each alone
  triggers the match, which is a *broader* match than the contract's literal phrase "in
  use by another") but ALSO adds a second, contract-unspecified rule: `"page"`,
  `"permission"`, or `"admin"` in the error text → `sem_acesso_admin_waba`, checked before
  falling back to the declared blocker. This is a reasonable, safe extension (it only ever
  resolves to an already-valid `blocker_reason`), not a regression, but it is additional
  behavior beyond what the contract froze — flagging it here since a future contract
  amendment should fold it in explicitly rather than have it live only in code.
- Everything else checked — `derive_initial_state`, `initial_next_retry_at`,
  `apply_config_status`, the `ativo` transition gate (`mode_resolved OR connected_at older
  than MODE_RESOLVE_FALLBACK_HOURS`), `harden_charge`'s Stripe payload shape, the metered
  line-item/trial checkout wiring, the internal endpoint shapes, and every new setting name
  + default — matched the contract exactly, no other deviations found.

## Migrations added this round (chain)

`0006_signup_intents` (pre-existing, now documented — cold signup) → `0007_onboarding_state_machine`
(this round: tenants state-machine columns + backfill, `signup_attempts`,
`users.professional_id`/invite-token columns, `entitlements.charge_hardened_at`,
`signup_intents.intake`) → `0008_billing_meter_pricing` (fully-metered pricing round:
`entitlements.cancel_scheduled_at`) ← head.

## Known gaps / pending (external wiring — nothing left to build in this repo's code)

- **Stripe**: no Price objects created yet for the fully-metered `secretaria_ferro` split
  (R$80/month per active professional + R$2/month per billable patient, both metered, NO
  fixed/anchor price), and `STRIPE_PRICE_MAP` has no `secretaria_ferro_metered_patients` /
  `secretaria_ferro_metered_professionals` key in any deployed env yet (the legacy single
  `secretaria_ferro_metered`-style key is still accepted but superseded — see the
  2026-07-19 update above); `STRIPE_METER_EVENT_BILLABLE_PATIENTS` and
  `STRIPE_METER_EVENT_ACTIVE_PROFESSIONALS` both still need real Meters configured in
  Stripe and wired into the deployed env. `STRIPE_TRIAL_PERIOD_DAYS`'s code default stays
  `0`/off, but per this round's business decision the deployed value is **75** days (long
  enough to outlast the 60-day retry window before Stripe would otherwise charge a
  still-unconnected tenant) — not independently verified against any live EasyPanel env
  (see "Not verified" below), but no longer an open question of WHAT the value should be.
- **Meta**: `META_APP_ID`/`META_APP_SECRET`/`META_ES_CONFIG_ID` are unset (empty-string
  defaults) in every env checked — the Embedded Signup token exchange and the
  `embedded_signup.configured` flag both degrade safely (fail-soft `'fail'` attempts /
  disabled-with-a-note frontend affordance) but the flow cannot go live until Meta
  tech-provider onboarding + an Embedded Signup config id exist.
  `NEXT_PUBLIC_META_APP_ID`/`NEXT_PUBLIC_META_ES_CONFIG_ID` are brain-frontend's mirror —
  out of this repo's scope.
- **SMTP**: brain-api never sends email directly — it enqueues via secretaria's
  `POST /internal/notifications/email`. Whether SMTP credentials/`EMAIL_ENABLED` are set
  is secretaria's concern (out of scope here); on this side, every email call is already
  fail-soft (`send_notification_email` returns `False` and the caller never blocks on it).
- **Deploy envs**: none of the settings verified above were checked against any deployed
  EasyPanel environment — only against `config.py` defaults and local test behavior. This
  doc does not assert they're set in staging/production.
- **Phase 2 (deliberately out of scope, per the contract's header)**: PreCheck-alone and
  combo Stripe Prices, PreCheck usage attribution. Not started; no code in this repo
  references it.
- **Partial-activation edge case** (flagged in the contract, confirmed still true in code):
  at `total_active >= 11` professionals, `config_status` can reach `'completa'` from a
  single complete professional (`PARTIAL_ACTIVATION_THRESHOLD = 10`, secretaria-side
  constant per the contract — not duplicated in brain-api). Since brain-api's config
  reminders are keyed off `tenants.config_status != 'completa'`
  (`onboarding_sync.list_onboarding_tenants`), a tenant past that threshold stops
  receiving config reminders for its remaining incomplete professionals once ONE is done.
  This is secretaria-side logic; brain-api only consumes the resulting `config_status`
  value, so this repo has nothing further to fix — noting it here since it affects how
  `GET /internal/onboarding/tenants` output should be interpreted downstream.
- **Frontend agenda professional-filter**: not implemented (brain-frontend scope, not
  this repo).
- **History webhook** (`history`/`smb_app_state_sync`): secretaria-side sync-state tracking
  only, no message-content ingestion — brain-api only ever sees the resulting
  `mode_resolved`/`connected_at` signal via the config-status pull, never the raw webhook.

## Not verified (cross-repo — flagged rather than assumed)

- The `professional_invite` email template's variable names. `api/onboarding.py` sends
  `{"name": ..., "clinic_name": ..., "link": invite_link}` with a comment asserting
  "secretaria's professional_invite template renders `{link}`" — this repo cannot confirm
  that secretaria's `services/email.py` template actually consumes exactly those three
  keys; that's secretaria's side of the contract.
- Everything on the secretaria side of the `/internal/tenants/*` surface (§4 of the
  contract) — `config-status`'s exact `professionals[]` shape, the `PARTIAL_ACTIVATION_THRESHOLD`
  constant's actual value, the cron cadence math — was cross-checked against the CONTRACT
  text and this repo's *consumption* of it, not against secretaria's own code.
- Whether any of the "Known gaps" settings above are actually set in a deployed EasyPanel
  environment (see above) — only local defaults/test behavior were checked.
