# Graph Report - .  (2026-09-16)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 2461 nodes · 5425 edges · 128 communities (120 shown, 8 thin omitted)
- Extraction: 88% EXTRACTED · 12% INFERRED · 0% AMBIGUOUS · INFERRED: 628 edges (avg confidence: 0.76)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `d82162c3`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 66|Community 66]]
- [[_COMMUNITY_Community 67|Community 67]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 70|Community 70]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 73|Community 73]]
- [[_COMMUNITY_Community 74|Community 74]]
- [[_COMMUNITY_Community 75|Community 75]]
- [[_COMMUNITY_Community 76|Community 76]]
- [[_COMMUNITY_Community 77|Community 77]]
- [[_COMMUNITY_Community 78|Community 78]]
- [[_COMMUNITY_Community 79|Community 79]]
- [[_COMMUNITY_Community 80|Community 80]]
- [[_COMMUNITY_Community 81|Community 81]]
- [[_COMMUNITY_Community 82|Community 82]]
- [[_COMMUNITY_Community 83|Community 83]]
- [[_COMMUNITY_Community 84|Community 84]]
- [[_COMMUNITY_Community 85|Community 85]]
- [[_COMMUNITY_Community 86|Community 86]]
- [[_COMMUNITY_Community 87|Community 87]]
- [[_COMMUNITY_Community 88|Community 88]]
- [[_COMMUNITY_Community 89|Community 89]]
- [[_COMMUNITY_Community 90|Community 90]]
- [[_COMMUNITY_Community 91|Community 91]]
- [[_COMMUNITY_Community 92|Community 92]]
- [[_COMMUNITY_Community 93|Community 93]]
- [[_COMMUNITY_Community 94|Community 94]]
- [[_COMMUNITY_Community 95|Community 95]]
- [[_COMMUNITY_Community 96|Community 96]]
- [[_COMMUNITY_Community 97|Community 97]]
- [[_COMMUNITY_Community 98|Community 98]]
- [[_COMMUNITY_Community 99|Community 99]]
- [[_COMMUNITY_Community 100|Community 100]]
- [[_COMMUNITY_Community 101|Community 101]]
- [[_COMMUNITY_Community 116|Community 116]]
- [[_COMMUNITY_Community 117|Community 117]]
- [[_COMMUNITY_Community 118|Community 118]]
- [[_COMMUNITY_Community 119|Community 119]]
- [[_COMMUNITY_Community 120|Community 120]]
- [[_COMMUNITY_Community 121|Community 121]]
- [[_COMMUNITY_Community 127|Community 127]]

## God Nodes (most connected - your core abstractions)
1. `_bearer()` - 170 edges
2. `_token()` - 165 edges
3. `Tenant` - 105 edges
4. `get_settings()` - 100 edges
5. `Entitlement` - 76 edges
6. `Principal` - 53 edges
7. `User` - 44 edges
8. `MessagePatientSession` - 40 edges
9. `_login()` - 39 edges
10. `_event()` - 37 edges

## Surprising Connections (you probably didn't know these)
- `test_login_returns_refresh_and_expiry()` --calls--> `get_settings()`  [INFERRED]
  tests/test_auth_hardening.py → src/brain_api/config.py
- `test_login_response_exposes_professional_id_and_name()` --calls--> `decode_token()`  [INFERRED]
  tests/test_auth_hardening.py → src/brain_api/core/security.py
- `test_login_response_professional_id_null_when_absent()` --calls--> `decode_token()`  [INFERRED]
  tests/test_auth_hardening.py → src/brain_api/core/security.py
- `test_secret_key_previous_rotation()` --calls--> `decode_token()`  [INFERRED]
  tests/test_auth_hardening.py → src/brain_api/core/security.py
- `test_quota_window_prefers_stripe_period_when_not_ended()` --calls--> `Entitlement`  [INFERRED]
  tests/test_precheck_billing.py → src/brain_api/models/entitlement.py

## Import Cycles
- None detected.

## Communities (128 total, 8 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.04
Nodes (72): Tenant cascade-delete tests (DELETE /admin/tenants/{id}).  Two layers: - Endp, The delete route is behind the same admin gate as the rest of /admin/*., A deleted clinic vanishes from every admin view and its owner can no longer log, test_delete_tenant_removes_clinic_everywhere(), test_delete_tenant_requires_admin(), test_delete_tenant_unknown_is_404(), test_checkout_requires_tenant_scoped_token(), Clinic A is seeded with the LEGACY plan string "precheck" (test_rbac.py), which (+64 more)

### Community 1 - "Community 1"
Cohesion: 0.04
Nodes (38): Tenant model — one clinic / organization on the Brain platform.  Non-sensitive, A clinic/organization. Owns users and a single entitlements row., Tenant, test_harden_charge_noop_when_no_entitlement_row(), A tenant created through the ORM is born with no channel enabled., A raw INSERT that never mentions the columns still lands `false`/`false`., `connected_at IS NOT NULL` is the whole predicate.      `services/onboarding.p, test_backfill_marks_only_tenants_with_a_connected_number() (+30 more)

### Community 2 - "Community 2"
Cohesion: 0.08
Nodes (57): MessagePatientAccount, MessagePatientSession, The revocable long leg of a patient session — the sibling of `refresh_tokens`., One patient account: the proven address, and nothing else.      Created by the, account_clinics(), add_clinic(), _adopt(), _as_utc() (+49 more)

### Community 3 - "Community 3"
Cohesion: 0.10
Nodes (51): _clear_config_status_cache(), _create_staff(), _noop_async(), Onboarding endpoints + integration vertical tests (ENDPOINTS + INTEGRATION + BIL, Corrections round (2026-07-22): invites are no longer owner-only — any doctor, Corrections round (2026-07-22): self-bind is no longer owner-only — any doctor, A secretaria outage during the post-commit bridge must NEVER break the webhook, `services.onboarding_sync._config_status_cache` is a module-level (per-process) (+43 more)

### Community 4 - "Community 4"
Cohesion: 0.07
Nodes (47): BoundLogger, _descreve(), main(), Cria (ou atualiza) um cupom de cortesia — acesso liberado sem passar pelo Stripe, get_logger(), Structured logging setup using structlog.  JSON output in non-dev environments, Blank any value whose key ends in `_encrypted` or looks secret-bearing., Configure structlog + stdlib logging. Safe to call more than once. (+39 more)

### Community 5 - "Community 5"
Cohesion: 0.11
Nodes (50): _body(), _body_ctx(), _entitled_tenant(), _FakeResponse, _install_fake_httpx(), _patch_entitlement(), Exception, MonkeyPatch (+42 more)

### Community 6 - "Community 6"
Cohesion: 0.07
Nodes (46): erasure(), export(), AsyncSession, LGPD cross-database privacy orchestration (CONTRACTS.md §14) — role `admin`., Erase a subject's data across brain + secretaria + precheck.      Idempotent:, Collect a subject's data across brain + secretaria + precheck.      Each servi, DemoRequest, DemoRequest model — isolated lead capture for the "Agendar demo" form.  Does N (+38 more)

### Community 7 - "Community 7"
Cohesion: 0.05
Nodes (42): join_launch_waitlist(), AsyncSession, Request, Public launch-waitlist endpoint (CONTRACTS.md §4 + §5): the PRE-LAUNCH buy gate., Capture a waitlist lead: honeypot + rate-limit guard, then upsert + confirm., WaitlistLead model — "avise-me quando lançar" capture for the pre-launch gate., A visitor who asked to be notified when the product goes on sale., WaitlistLead (+34 more)

### Community 8 - "Community 8"
Cohesion: 0.06
Nodes (46): Cold-signup vertical tests: register-at-first-card, Checkout Session, onboarding, POST /doctor/onboarding/intake is gated by require_doctor (401 with no token)., The authenticated mid-wizard call attaches the intake to the caller's pending in, GET /public/checkout-config echoes the REAL deployed STRIPE_TRIAL_PERIOD_DAYS —, The core fix: after the first card, the owner can log in normally with the passw, `addons` covers every `catalog.ADDON_IDS` id in stable alphabetical order;, A freshly registered, unpaid tenant resolves cleanly to a coherent "nothing, Register a lead (first card) and return its intent_id. (+38 more)

### Community 9 - "Community 9"
Cohesion: 0.09
Nodes (29): appointments(), doctor_me(), doctor_me_update(), patients(), AsyncSession, Doctor (tenant) endpoints (RBAC task, Part 1B) — `auth-jwt-multitenant` skill., Mint the tenant-scoped, purpose-scoped token the portal presents to secretarIA's, The authenticated doctor's profile + tenant + entitlements (no secrets). (+21 more)

### Community 10 - "Community 10"
Cohesion: 0.07
Nodes (45): BaseSettings, Strongly-typed settings. Values come from the environment or `.env`.      Real, Parse CORS_ALLOW_ORIGINS into a clean list of origins.          Accepts a JSON, Settings, apply_onboarding_event(), _ConfigStatusCacheEntry, ensure_precheck_provisioned(), ensure_secretaria_provisioned() (+37 more)

### Community 11 - "Community 11"
Cohesion: 0.06
Nodes (46): checkout(), portal(), precheck_topup(), precheck_upgrade(), precheck_usage(), _precheck_usage_out(), AsyncSession, CheckoutSessionOut (+38 more)

### Community 12 - "Community 12"
Cohesion: 0.11
Nodes (45): PatientConsentEvent, LGPD consent/legal-basis audit trail for a Brain-Message patient.      Shaped, `POST /refresh` as the portal sends it. With `cookie`, that exact value is sent, _refresh(), _threads(), _add(), _count(), _email_login() (+37 more)

### Community 13 - "Community 13"
Cohesion: 0.09
Nodes (44): _extended_fake_settings(), _link_stripe_customer(), SimpleNamespace, Billing Phase 1 / fully-metered vertical tests (CONTRACT_onboarding_v1.md §9): t, Same forwarding mechanics as billable_patients, dispatched to the DIFFERENT, Same forwarding mechanics as billable_patients/active_professionals, dispatched, The cold-signup checkout builder no longer carries ANY of the fully-metered shap, `_parse_price_map` must accept `{plan_id}_metered_patients`,     `{plan_id}_met (+36 more)

### Community 14 - "Community 14"
Cohesion: 0.06
Nodes (33): Protocol, check_quota(), Decision, EntitlementLike, is_entitled(), UUID, Entitlement resolution service (stripe-billing-entitlements skill, CONTRACTS.md, Outcome of a pure, in-process quota check (mirrors the skill).      Scaffold f (+25 more)

### Community 15 - "Community 15"
Cohesion: 0.09
Nodes (40): generate_refresh_token(), hash_refresh_token(), JWT issuance/validation and password hashing (the identity-authority primitives), A high-entropy opaque token (the client-side value; only its hash is stored)., SHA-256 hex for storage/lookup. Fast is fine: the input is 64 random bytes, verify_password(), User model — a person who logs into the Brain portal.  `password_hash` (bcrypt, A portal user. `tenant_id` is NULL for a platform admin. (+32 more)

### Community 16 - "Community 16"
Cohesion: 0.09
Nodes (42): _apply_precheck_topup_checkout(), apply_stripe_event(), _apply_trial(), create_checkout_session(), create_portal_session(), create_precheck_topup_checkout_session(), _create_subscription_for_signup(), _entitlement_for_event() (+34 more)

### Community 17 - "Community 17"
Cohesion: 0.10
Nodes (42): _cookie_value(), _login(), The HttpOnly refresh cookie: emission, the cookie-first refresh, the CSRF header, A client presenting both is one caught mid-deploy; the cookie holds the leg, The un-migrated portal must keep working, header-less, for the whole round., 401, not the 422 an unconditionally-required body would give: 'no credential', A cross-site form POST carries the cookie but cannot set a header. This check, The guard must fire BEFORE rotation — otherwise a forged request still burns (+34 more)

### Community 18 - "Community 18"
Cohesion: 0.10
Nodes (41): Principal, _action_out(), attach_intake(), bind_self_professional(), _embedded_signup_out(), get_onboarding(), get_test_window(), invite_professional() (+33 more)

### Community 19 - "Community 19"
Cohesion: 0.10
Nodes (40): _bearer(), _configure_mesh(), _login(), Full request -> verify round trip. Returns the `verify-otp` response., Replace `httpx.AsyncClient.request` and record every outbound hop.      A spy, Point both upstreams somewhere and give each its OWN secret.      Two DIFFEREN, The scope claim is the wall: a patient session is refused by `get_current_princi, `products.precheck == false` means no PreCheck tab — not a disabled one. (+32 more)

### Community 20 - "Community 20"
Cohesion: 0.12
Nodes (38): _set_pair_key(), test_internal_onboarding_events_unknown_tenant_404(), test_internal_onboarding_tenants_key_gate(), _due_entitlement(), _due_tenant(), _link_and_set_plan(), _noop_async(), SimpleNamespace (+30 more)

### Community 21 - "Community 21"
Cohesion: 0.08
Nodes (34): BaseModel, AttemptIn, AttemptOut, EmbeddedSignupOut, LastAttemptOut, OnboardingStateOut, PauseIn, ProfessionalInviteIn (+26 more)

### Community 22 - "Community 22"
Cohesion: 0.09
Nodes (35): SignupIntent model — cold-signup lead through Stripe Checkout to entitlement act, One cold-signup attempt: lead data + catalog selection through to provisioning., SignupIntent, _apply_signup_intent_checkout(), _fail_intent(), Mark a cold-signup intent permanently failed, WITHOUT raising and WITHOUT     a, `checkout.session.completed` for a cold signup: CREATE the subscription, then, _as_utc() (+27 more)

### Community 23 - "Community 23"
Cohesion: 0.12
Nodes (34): _account_body(), add_clinic_by_invite(), _authenticate_account(), _authenticate_patient(), _bearer_value(), _clinic_session(), confirm_sibling(), get_current_account() (+26 more)

### Community 24 - "Community 24"
Cohesion: 0.09
Nodes (34): create_patient_token(), decode_patient_account_token(), decode_patient_token(), decode_token(), Any, Return the claims, or None for any invalid/expired/forged token.      `algorit, Mint the short-lived access token for one Brain-Message patient session., Validate a patient token: signature/expiry via `decode_token` + the EXACT scope. (+26 more)

### Community 25 - "Community 25"
Cohesion: 0.08
Nodes (33): Entitlement, Entitlement model — one row per tenant (stripe-billing-entitlements skill).  T, Per-tenant entitlement state. Primary-keyed by tenant_id (one row per tenant)., _live_subscription(), A fake `_stripe_get` returning a live subscription snapshot (FIX 1b)., Schedules using the LIVE trial_end (from `_stripe_get`), NOT the event's own --, FIX 1b: the LIVE subscription is the authority. Even though the cheap     short, The live trial_end must be more than an hour out; one ~now (about to end     an (+25 more)

### Community 26 - "Community 26"
Cohesion: 0.09
Nodes (31): delete_tenant(), get_admin_anamnesis(), get_admin_metrics(), get_secretaria_tenants(), get_tenant(), get_tenant_entitlements(), impersonate_token(), list_admin_anamneses() (+23 more)

### Community 27 - "Community 27"
Cohesion: 0.14
Nodes (31): build_session_response(), _check_auth_rate_limit(), exchange_invite_token(), exchange_onboarding_token(), issue_session(), login(), logout(), me() (+23 more)

### Community 28 - "Community 28"
Cohesion: 0.09
Nodes (25): create_hub_token(), decode_hub_token(), Mint the tenant-scoped token the doctor portal presents to secretarIA's hub., Validate a hub token: signature/expiry via `decode_token` + the exact scope., Auth-hardening round tests: refresh rotation/reuse, rate limiting, password poli, SECRETARIA_API_KEY is forced empty by conftest -> fail closed with 403., _tenant_ids(), test_auth_me_exposes_professional_id_when_present() (+17 more)

### Community 29 - "Community 29"
Cohesion: 0.08
Nodes (25): email_spy(), _EmailSpy, Password-reset round tests: the flow brain-api never had.  Ground truth: api/a, Same status AND same body for a registered and an unregistered address.      I, A syntactically invalid address 422s instead of getting the generic 200., Users are stored lower-cased; a capitalised address must still find them —, Confirm burns the token — a replayed link cannot set a second password., Only one live link per user: the second request overwrites the stored hash. (+17 more)

### Community 30 - "Community 30"
Cohesion: 0.10
Nodes (29): _confirm(), _confirm_url(), _link_events(), Patient access by e-mail OTP + the product switchboard (Brain-Message, piece 7)., A copied access token, replayed from another browser, links nothing., Keyed by the authenticated address: a fresh X-Forwarded-For buys nothing, and, The budget is counted only after the cookie and recency checks, so a leaked toke, The cookie is ambient, so the CSRF guard runs first — a forged POST burns nothin (+21 more)

### Community 31 - "Community 31"
Cohesion: 0.12
Nodes (29): _bearer(), _noop_async(), `secretary` role tests (secretary round, 2026-08-14).  The role is defined as, The constant is wired into the canonical tuple, not just defined next to it., A secretary authenticates like anyone else; the claim survives into the JWT., Every day-to-day surface answers a secretary token: profile, the clinic's agenda, `require_owner` accepts `secretary` as an ALTERNATIVE to `is_owner` — without, The invite mirrors the professional one EXCEPT for the secretaria round-trip: no (+21 more)

### Community 32 - "Community 32"
Cohesion: 0.19
Nodes (26): _admin_entitlements(), _event(), _FakeResponse, A fully-metered secretaria_basico subscription has NO anchor/flat price item at, test_subscription_updated_fully_metered_companions_only_resolves_plan(), _post_webhook(), Stripe billing vertical tests (webhook apply + checkout/portal endpoints).  Gr, Even a signature valid against the REAL secret is rejected when the configured (+18 more)

### Community 33 - "Community 33"
Cohesion: 0.08
Nodes (24): create_demo_request_endpoint(), AsyncSession, Request, Public demo-request endpoint (CONTRACTS.md §4 + §5): "Agendar demo" lead capture, Capture a demo request: honeypot + rate-limit guard, then persist + confirm., DemoRequestConfirmation, DemoRequestCreate, ProductInterestEnum (+16 more)

### Community 34 - "Community 34"
Cohesion: 0.09
Nodes (25): PrecheckTopupCredit, PreCheck top-up credit ledger — avulso consultations purchased on top of a PreCh, One granted PreCheck avulso purchase (`checkout.session.completed`,     `metada, PreCheck billing vertical tests (precheck-billing round).  Ground truth: servi, PRECHECK_API_KEY is unset by default (conftest never sets it) -> fail closed., precheck_consultations is flat+quota billing, never metered — record_usage must, The grant is EXACTLY the purchased quantity (12 here, deliberately not a round, Belt-and-braces guard: a DIFFERENT event.id completing the SAME Checkout Session (+17 more)

### Community 35 - "Community 35"
Cohesion: 0.08
Nodes (24): list_onboarding_tenants(), secretaria's `run_onboarding_nudges` cron pulls this hourly to decide who needs, HubTokenVerifyIn, HubTokenVerifyOut, InternalEntitlementOut, InternalOnboardingEventIn, InternalOnboardingEventOut, InternalOnboardingListOut (+16 more)

### Community 36 - "Community 36"
Cohesion: 0.10
Nodes (23): checkout_config(), No rate limit — deliberate. Unlike the routes above, this touches no DB and, The brain-api session pair. Shape mirrors PreCheck's `TokenResponse` and the, TokenResponse, AddonAvailabilityOut, CheckoutConfigOut, CheckoutSessionCreate, CheckoutSessionOut (+15 more)

### Community 37 - "Community 37"
Cohesion: 0.13
Nodes (25): get_settings(), Return a cached Settings instance (read once per process)., create_patient_account_token(), Mint the short-lived token of one patient ACCOUNT login.      `sub` is `messag, _empty_page(), get_admin_anamnesis(), get_admin_metrics(), get_anamnesis() (+17 more)

### Community 38 - "Community 38"
Cohesion: 0.08
Nodes (23): ExchangeInviteTokenIn, ExchangeOnboardingTokenIn, LoginRequest, LogoutRequest, MeResponse, MessageOut, PasswordResetConfirmIn, PasswordResetRequestIn (+15 more)

### Community 39 - "Community 39"
Cohesion: 0.13
Nodes (24): _FakeResponse, _install_fake_meta_httpx(), _install_fake_provisioning_httpx(), _install_fake_waba_subscribe_httpx(), Exception, MonkeyPatch, SimpleNamespace, (b) subscribe_app_to_waba success — hits the right path, Bearer header only, no (+16 more)

### Community 40 - "Community 40"
Cohesion: 0.21
Nodes (22): _admin_token(), _FakeResponse, _install_fake_httpx(), _privacy_requests(), Exception, MonkeyPatch, LGPD cross-database privacy orchestration tests (CONTRACTS.md §14).  Ground tr, Point services/privacy at a configured mesh + a fake httpx routed by base_url. (+14 more)

### Community 41 - "Community 41"
Cohesion: 0.09
Nodes (21): ClinicInviteIn, ConfirmSiblingIn, MessageOut, OtpRequestIn, OtpVerifyIn, PatientMessageIn, Schemas for the patient side of the Brain-Message channel (account model, 2026-0, DEPRECATED (transition): `POST /patient-access/siblings/{tenant_id}/confirm`. (+13 more)

### Community 42 - "Community 42"
Cohesion: 0.11
Nodes (20): _precheck_key_scheme, create_precheck_usage_event(), get_precheck_quota(), AsyncSession, description, Path, Security, UUID (+12 more)

### Community 43 - "Community 43"
Cohesion: 0.16
Nodes (21): _bulk_delete(), _delete_secretaria_tenant(), delete_tenant(), _entitlement_out(), get_entitlement(), get_tenant_detail(), list_tenants(), AsyncSession (+13 more)

### Community 44 - "Community 44"
Cohesion: 0.10
Nodes (20): create_admin(), _parse_args(), Namespace, Create (or promote) a platform-admin user — full access to the /admin/* surface., AsyncSession, Create the platform admin if it does not already exist. Idempotent.      Crede, seed_admin(), hash_password() (+12 more)

### Community 45 - "Community 45"
Cohesion: 0.17
Nodes (20): _check_rate_limit(), create_checkout_session(), onboarding_status(), AsyncSession, CheckoutSessionOut, Request, Response, UUID (+12 more)

### Community 46 - "Community 46"
Cohesion: 0.13
Nodes (21): The fully-metered shape (all three companion prices, no quantity on any of them), test_signup_webhook_subscription_carries_all_three_metered_companions_and_trial(), _install_setup_mode_stripe_fakes(), _intent_row(), Fake `billing._stripe_get` / `billing._stripe_post` as plain async functions and, Re-read a `SignupIntent` straight from the `client` fixture's DB (a fresh sessio, A REAL `mode=setup` `checkout.session.completed` object: it carries `setup_inten, End-to-end of the new flow: a `mode=setup` completion resolves the saved card of (+13 more)

### Community 47 - "Community 47"
Cohesion: 0.14
Nodes (19): AdminDemoRequestPatchIn, AdminTenantDeleteOut, AdminTenantDetailOut, AdminTenantOut, EntitlementAdminOut, ImpersonationTokenOut, Pydantic v2 schemas for the platform-admin vertical (RBAC task, Part 1A).  Eve, Move a demo request along the pipeline.      The portal's three row actions (" (+11 more)

### Community 48 - "Community 48"
Cohesion: 0.14
Nodes (19): AddonDef, compute_entitlement_state(), compute_limits(), default_addons(), get_addon(), get_plan(), plan_tier(), PlanDef (+11 more)

### Community 49 - "Community 49"
Cohesion: 0.21
Nodes (17): _FakeResponse, _install_fake_httpx(), Exception, MonkeyPatch, brain-api -> secretaria internal data path (/doctor/appointments + /doctor/patie, secretaria's 401 (key mismatch) becomes a clean 502 — body never leaks to the do, secretaria's 403 (its OWN key unset) is an unconfigured mesh => empty page, not, Point the client at a configured mesh and a fake httpx that records the call. (+9 more)

### Community 50 - "Community 50"
Cohesion: 0.12
Nodes (18): MessagePatientAccountOtp, The pending e-mail challenge for one ADDRESS — at most one, ever.      The acc, _peek_code(), Brute-force the code out of its HASH — the test's stand-in for reading the e-mai, Just enough of `httpx.Response` for `message_switchboard._call`., A wrong guess is a 400 with the generic message, and it is COUNTED., Past `expires_at` the code is dead even though it is the RIGHT code., The second presentation of a correct code fails — `consumed_at` is burned. (+10 more)

### Community 51 - "Community 51"
Cohesion: 0.14
Nodes (18): apply_config_status(), derive_initial_state(), initial_next_retry_at(), map_error_to_blocker(), provision_defaults(), AsyncSession, datetime, UUID (+10 more)

### Community 52 - "Community 52"
Cohesion: 0.19
Nodes (18): activate_tenant(), connect_whatsapp(), create_professional(), get_config_status(), provision_tenant(), Any, Response, UUID (+10 more)

### Community 53 - "Community 53"
Cohesion: 0.17
Nodes (17): EntitlementOut, `GET /entitlements` payload — resolved entitlement state for one tenant., available_products(), _call(), list_messages(), Any, UUID, The switchboard — one patient session, two product backends, zero text classific (+9 more)

### Community 54 - "Community 54"
Cohesion: 0.14
Nodes (17): _expired_patient_cookie_headers(), MessageOut, Request, Response, The `Set-Cookie` that deletes the patient cookie, as raise-able headers., Begin the login of the ACCOUNT.      TWO limiters before any DB work: the per-, Burn the code, open (or create) the account, add the invited clinic, open the se, The silent renewal behind "opened the portal, already logged in".      The coo (+9 more)

### Community 55 - "Community 55"
Cohesion: 0.12
Nodes (16): ChannelsOut, PatientInviteOut, ProductsOut, Pydantic v2 schemas for the entitlements vertical (CONTRACTS.md §3.1).  The re, Per-product access flags (CONTRACTS.md §3.1 `products`).      `precheck` <- en, Per-channel delivery flags (CONTRACTS.md §3.1 `channels`).      `whatsapp` <-, `GET /entitlements/patient-invite` — what a clinic hands its patients so they ca, AsyncSession (+8 more)

### Community 56 - "Community 56"
Cohesion: 0.19
Nodes (16): chamadas(), Tests de `onboarding_sync.ensure_precheck_provisioned` — o bridge que faz "pago, A frase é o que o paciente vê no QR impresso na recepção e o que digita     qua, Um nome que não sobrevive à normalização não pode virar "precheck " solto —, Captura as chamadas ao PreCheck e deixa o teste escolher a resposta., _tenant_com_precheck(), test_excecao_inesperada_e_engolida(), test_falha_do_precheck_nao_levanta_e_nao_carimba() (+8 more)

### Community 57 - "Community 57"
Cohesion: 0.17
Nodes (13): link(), _parse_args(), Namespace, Link a brain user to their PreCheck user id — the SSO account-link (onboarding)., _resolve_user(), ensure_demo_link(), main(), Seed a development tenant + owner user + entitlement.  Idempotent: re-running (+5 more)

### Community 58 - "Community 58"
Cohesion: 0.15
Nodes (15): _expired_cookie_headers(), The `Set-Cookie` that deletes the refresh cookie, as raise-able headers., clear_patient_session_cookie(), clear_refresh_cookie(), Request, Response, The HttpOnly refresh-token cookie, and the CSRF header that guards it.  WHY TH, Expire the cookie in the browser (logout, or a rejected rotation).      The at (+7 more)

### Community 59 - "Community 59"
Cohesion: 0.23
Nodes (15): internal_entitlements(), internal_professional_emails(), post_onboarding_event(), precheck_handoff(), AsyncSession, description, Path, UUID (+7 more)

### Community 60 - "Community 60"
Cohesion: 0.13
Nodes (14): precheck_sso_token(), AsyncSession, Mint a PreCheck session for the authenticated tenant's linked user.      `requ, create_precheck_token(), Mint a token that PreCheck's OWN auth will accept (the SSO handoff).      PreC, PrecheckSsoTokenResponse, SSO handoff schemas (CONTRACTS.md §5).  The response of POST /sso/precheck/tok, A minted PreCheck session token + how long it is valid (seconds). (+6 more)

### Community 61 - "Community 61"
Cohesion: 0.12
Nodes (3): End-to-end smoke test of the full HTTP contract (CONTRACTS.md).  Runs the real, A filled honeypot returns 201 but persists nothing., test_demo_request_honeypot_drops_silently()

### Community 62 - "Community 62"
Cohesion: 0.13
Nodes (13): DeclarativeBase, Base, Declarative base shared by every ORM model., ProcessedStripeEvent, Stripe webhook idempotency ledger (stripe-billing-entitlements skill).  Stripe, A Stripe event id we already applied. PK = the event id (natural dedupe key)., Usage-event ledger — one row per recorded internal usage event (metering).  Pe, A single applied usage event. PK = the caller's idempotency key (natural dedupe) (+5 more)

### Community 63 - "Community 63"
Cohesion: 0.13
Nodes (12): In-process sliding-window rate limiter (CONTRACTS.md §5).  Deliberately trivia, Per-key (client IP) sliding 60s window. `limit_getter` is read per call so a, True if `key` is under the limit (and records the hit), else False.          F, SlidingWindowLimiter, test_login_rate_limited(), Second call from the same IP inside the window -> 429 (limit forced to 1)., test_rate_limit_trips(), Unlike the three signup routes, this endpoint deliberately does NOT share the (+4 more)

### Community 64 - "Community 64"
Cohesion: 0.14
Nodes (13): IntakeIn, Same rule as SetPasswordIn/AdminUserCreateIn: reject a purely-numeric or, Pre-checkout intake answers (CONTRACT_onboarding_v1.md §7).      Stored verbat, Body for `POST /public/signup-intents` — the FIRST-card lead + selection + passw, SignupIntentCreate, The result of registering a lead: the intent (drives checkout) + the owner user, Registration, register (no intake) -> attach_intake -> provision reads the attached intake and (+5 more)

### Community 65 - "Community 65"
Cohesion: 0.21
Nodes (14): _admin_request(), delete_tenant(), list_tenants(), Any, Response, UUID, Service-to-service admin client into secretaria (the cross-API admin connection), Extract a safe `detail` string from an upstream error response. (+6 more)

### Community 66 - "Community 66"
Cohesion: 0.22
Nodes (14): Tests for `GET /internal/tenants/{tenant_id}/professional-emails`.  The read t, A tenant with nobody linked and a tenant that does not exist answer the     sam, Asking about tenant B never returns tenant A's address.      The isolation inv, SECRETARIA_API_KEY is forced empty by conftest -> fail closed with 403.      A, Tenant B's owner has no `professional_id`.      Absent, not present-with-null:, _set_pair_key(), _tenant_ids(), test_linked_professional_is_returned() (+6 more)

### Community 67 - "Community 67"
Cohesion: 0.15
Nodes (12): create_user(), Create a user in any tenant with any role (how we onboard doctor accounts)., AdminUserCreateIn, AdminUserOut, A user row for the admin users table. NEVER declares `password_hash`.      `cl, Create a user in any tenant, with any role (admin tooling, Part 1A).      Pass, Minimum composition: length is enforced by the Field; require letter + digit, A platform admin has no tenant, and cannot also be marked owner/manager (those (+4 more)

### Community 68 - "Community 68"
Cohesion: 0.16
Nodes (12): create_usage_event(), The inbound metering write path (stripe-billing-entitlements: METERING leg only, `POST /internal/usage-events` body — one billable/meterable action, already done, `POST /internal/usage-events` response. `recorded=False` means a duplicate, UsageEventIn, UsageEventOut, _forward_meter_event(), AsyncSession (+4 more)

### Community 69 - "Community 69"
Cohesion: 0.14
Nodes (12): Doctor self-edit profile tests (`PATCH /doctor/me`, "Meu Perfil" foundation roun, Same router-level require_doctor gate as every other /doctor/* route: 403 for ad, Editing as Owner A never touches Owner B's row (tenant scoping via the token)., Happy path: name changes, everything else on the response is unchanged., Whitespace-only input min_length(1) would let through is still rejected (422)., The schema's extra="forbid" rejects email/role/tenant_id — not a manual filter., test_update_does_not_leak_across_tenants(), test_update_name_blank_rejected() (+4 more)

### Community 70 - "Community 70"
Cohesion: 0.21
Nodes (9): FastAPI, health(), Health check endpoint., Liveness probe. Intentionally does not touch Postgres., SSO endpoint (CONTRACTS.md §5, auth-jwt-multitenant skill).  `POST /sso/preche, create_app(), lifespan(), FastAPI application entrypoint.  Run with:     uvicorn brain_api.main:app --h (+1 more)

### Community 71 - "Community 71"
Cohesion: 0.21
Nodes (12): _append_checkout_line_items(), _append_subscription_items(), _apply_setup_custom_text(), CheckoutSelection, A validated purchase: one assignable plan + optional extra add-ons., The ordered `(price_id, quantity)` list a validated selection implies — the ONE, Populate `line_items[i][price]`/`[quantity]` for a validated selection, shared b, Populate `items[i][price]`/`[quantity]` for a validated selection — the subscrip (+4 more)

### Community 72 - "Community 72"
Cohesion: 0.29
Nodes (11): _empty_page(), _get_list(), list_appointments(), list_patients(), Any, UUID, Internal service-to-service DATA client into secretaria (`X-Internal-Api-Key`)., Tenant appointments — `GET /internal/tenants/{tenant_id}/appointments`.      ` (+3 more)

### Community 73 - "Community 73"
Cohesion: 0.17
Nodes (12): _install_fake_stripe_httpx(), Without a `{plan}_metered` STRIPE_PRICE_MAP key and with STRIPE_TRIAL_PERIOD_DAY, test_existing_tenant_checkout_appends_all_three_metered_companions_and_trial(), test_existing_tenant_checkout_no_metered_price_no_extra_line_item(), Point services.billing at fake Stripe settings + a recording fake httpx client., The chosen quantity drives BOTH the line item (so Stripe charges quantity x unit, Below the minimum is refused SERVER-SIDE, whatever the frontend allows -- and, The minimum itself is INSIDE the allowed range (boundary, not an off-by-one). (+4 more)

### Community 74 - "Community 74"
Cohesion: 0.20
Nodes (10): get_entitlements(), get_patient_invite(), AsyncSession, Entitlements endpoint (CONTRACTS.md §3.1, stripe-billing-entitlements skill)., Return the resolved entitlement state for the authenticated tenant.      `requ, The clinic's own patient invite: its short code and link (Brain-Message portal)., generate_invite_code(), invite_link() (+2 more)

### Community 75 - "Community 75"
Cohesion: 0.18
Nodes (11): A valid tenant with no entitlement row gets coherent defaults, not a 404., A tenant without PreCheck entitlement -> 403 precheck_not_entitled., Entitled but unlinked brain user -> 409 precheck_account_not_linked., A tenant_owner (non-admin) is rejected by the router gate before any upstream ca, test_entitlements_default_when_missing(), test_entitlements_entitled_tenant(), test_me_identity_only(), test_secretaria_proxy_requires_admin_role() (+3 more)

### Community 76 - "Community 76"
Cohesion: 0.22
Nodes (10): get_current_principal(), Turn an `Authorization: Bearer <jwt>` header into a validated Principal., create_access_token(), Mint a short-lived access token. `sub` is the brain user id (UUID string)., A malformed claim must never 401 the whole token — it just reads as absent., test_access_token_omits_professional_id_when_none(), test_access_token_round_trips_professional_id(), test_principal_parses_valid_professional_id_claim() (+2 more)

### Community 77 - "Community 77"
Cohesion: 0.20
Nodes (10): _admin_token(), A platform-admin JWT (role=admin, no tenant). No admin user row is needed: the a, Admin role passes the gate; with no SECRETARIA_BASE_URL/token, fail closed (503), The brain-api route's own guard rejects confirm=false BEFORE touching secretaria, confirm=true but no upstream configured -> 503 (never silently 'succeeds')., When the upstream client returns data, the route relays it unchanged.      Pat, test_secretaria_reset_requires_confirm(), test_secretaria_reset_unconfigured_is_503() (+2 more)

### Community 78 - "Community 78"
Cohesion: 0.22
Nodes (6): Seed the single platform admin user (idempotent).  Reads ADMIN_EMAIL / ADMIN_P, Application configuration loaded from environment variables / .env file., get_session(), AsyncSession, Async SQLAlchemy 2.0 engine, session factory and declarative Base., FastAPI dependency that yields a database session.

### Community 79 - "Community 79"
Cohesion: 0.36
Nodes (8): normalize_invite_code(), parse_invite(), UUID, The clinic's patient invite: a short code a person can type, and the parser that, The stored spelling of a typed code, or `None` when it cannot be one., A clinic UUID, a normalized code, or `None` — never an error.      Anything sh, _single(), test_the_parser_never_guesses()

### Community 80 - "Community 80"
Cohesion: 0.31
Nodes (8): provision_clinic(), Any, Response, UUID, Internal service-to-service PROVISIONING client into PreCheck (`X-Internal-Token, Call `method path` on PreCheck's internal surface with the shared pair key., `POST /internal/provision` — create (or idempotently confirm) the PreCheck clini, _request()

### Community 81 - "Community 81"
Cohesion: 0.22
Nodes (7): db_session(), Test configuration.  pytest imports conftest before any test module, so config, A bare `AsyncSession` over a FRESH in-memory SQLite DB (all tables created)., pclient(), The tenant ids the fixture created, by role in the tests., `(client, sessionmaker, seed)` over a fresh in-memory DB with the three clinics., _Seed

### Community 82 - "Community 82"
Cohesion: 0.22
Nodes (8): MonkeyPatch, Admin "Modo médico" impersonation (POST /admin/impersonate/token).  Reuses the, The mint is admin-only: 403 for a tenant token, 401 with no token., With the default demo email (not seeded in the test DB), an admin gets a clean 4, A configured target yields a real tenant-scoped doctor token (not the admin's)., test_impersonate_mints_working_doctor_token(), test_impersonate_requires_admin(), test_impersonate_target_unavailable_404()

### Community 83 - "Community 83"
Cohesion: 0.44
Nodes (8): _alembic(), _guard(), Migration 0020 — and the Postgres-only statements behind it — on a REAL Postgres, _refresh_with(), _rows(), _sha(), test_0020_keeps_every_handle_and_what_an_old_cookie_reopens(), test_the_postgres_only_statements_hold_under_real_concurrency()

### Community 84 - "Community 84"
Cohesion: 0.36
Nodes (8): O trial é por PLANO, não global (`billing._trial_days_for`).  `STRIPE_TRIAL_PE, A página de pagamento do PreCheck não pode falar de aprovação da Meta., _sel(), test_com_trial_o_texto_aparece(), test_plano_com_secretaria_mantem_o_trial(), test_plano_desconhecido_nao_tem_trial(), test_precheck_nao_tem_trial(), test_sem_trial_nao_ha_texto_de_espera_na_pagina()

### Community 85 - "Community 85"
Cohesion: 0.29
Nodes (7): _do_run_migrations(), Connection, Alembic environment — async (asyncpg) configuration., Run migrations in 'offline' mode (emit SQL, no live DB connection)., Run migrations in 'online' mode using an async engine., run_migrations_offline(), run_migrations_online()

### Community 86 - "Community 86"
Cohesion: 0.25
Nodes (8): catalog_id_for_price(), _parse_price_map(), price_id_for(), Parse STRIPE_PRICE_MAP (keyed by the raw string so a settings monkeypatch in, The Stripe price id selling a catalog plan/add-on (or a `{plan_id}_metered_patie, Reverse lookup: which catalog id a Stripe price id sells (webhook recompute)., Resolve + validate a purchase request against the catalog and the price map., validate_selection()

### Community 87 - "Community 87"
Cohesion: 0.43
Nodes (5): _backfill_accounts(), _backfill_invite_codes(), Connection, patient accounts: login só por e-mail + clínica entra na conta por convite  Re, upgrade()

### Community 88 - "Community 88"
Cohesion: 0.33
Nodes (5): downgrade(), secretary role: add `secretary` to the tenant role taxonomy (NO-OP by design), Intentionally empty — see the module docstring., Intentionally empty.      Deliberately does NOT delete or rewrite `secretary`, upgrade()

### Community 89 - "Community 89"
Cohesion: 0.33
Nodes (5): Proxy secretaria's destructive data wipe (brain-api -> secretaria `/admin/reset`, reset_secretaria(), Request schema for the brain-api -> secretaria admin proxy (CONTRACTS.md §11)., Body for `POST /admin/secretaria/reset` — a DESTRUCTIVE data wipe on secretaria., SecretariaResetIn

### Community 90 - "Community 90"
Cohesion: 0.33
Nodes (3): EntitlementPatchIn, Partial update of a tenant's entitlement (admin manual activation, pre-Stripe)., Only assignable catalog plans may be written (legacy aliases normalize).

### Community 91 - "Community 91"
Cohesion: 0.33
Nodes (5): exchange_code_for_token(), Meta Graph API client — WhatsApp Embedded Signup token exchange + WABA webhook, `GET {META_GRAPH_BASE_URL}/oauth/access_token` — returns the access token, or, `POST {META_GRAPH_BASE_URL}/{waba_id}/subscribed_apps` — subscribes this app to, subscribe_app_to_waba()

### Community 92 - "Community 92"
Cohesion: 0.40
Nodes (5): Any, UUID, secretarIA -> brain-api -> PreCheck patient handoff (CONTRACTS.md §12.3, one leg, POST PreCheck's `/internal/precheck-handoff`; caller has ALREADY confirmed the, request_handoff()

### Community 93 - "Community 93"
Cohesion: 0.50
Nodes (4): _internal_key_scheme, Security, Gate `/internal/*` on the shared pair key. Fail CLOSED; never log the candidate., require_internal_api_key()

### Community 97 - "Community 97"
Cohesion: 0.50
Nodes (3): MessagePatientOtp, Patient-access models — the PATIENT's end of the Brain-Message channel.  Every, LEGACY (0018): the per-clinic challenge of the pre-account login.      Nothing

### Community 98 - "Community 98"
Cohesion: 0.50
Nodes (4): AdminDemoRequestOut, A demo/lead row for the admin demo-requests table (brain's own table)., list_demo_requests(), Return one page of demo requests (newest first) + the total count.

### Community 99 - "Community 99"
Cohesion: 0.50
Nodes (4): channel_open_tenant(), The open clinic a pasted link, short code or UUID names, or `None` for every fai, The tenant, iff it exists AND has the Brain-Message channel switched on., resolve_invite()

### Community 100 - "Community 100"
Cohesion: 0.50
Nodes (4): _force_intent_pending(), Rewind an intent to `pending_payment` in the DB, simulating the ONE case a Strip, A re-run of this handler for the SAME signup intent can never mint a SECOND, test_setup_webhook_subscription_create_is_idempotent_on_redelivery()

### Community 101 - "Community 101"
Cohesion: 0.50
Nodes (4): _decode_like_precheck(), Validate the token EXACTLY as PreCheck does — not a mock.      Mirrors PreChec, Entitled + linked brain user -> a token PreCheck's own auth accepts., test_sso_precheck_token_success()

### Community 116 - "Community 116"
Cohesion: 0.67
Nodes (3): client_ip(), Request, Best-effort client IP for a per-IP limiter key.      The service runs behind n

### Community 127 - "Community 127"
Cohesion: 0.12
Nodes (15): deny_secretary(), Shared FastAPI auth dependencies (auth-jwt-multitenant skill).  The token is v, Require a tenant-scoped user of the operational clinic portal (`doctor`,     `m, Require the doctor-scoped principal to be the tenant OWNER — or a `secretary`., Raise 403 `error_code` when `p` is a `secretary`; a no-op for every other role., Dependency factory: 403 unless the caller's role is allowed., Require a tenant-scoped principal (a token that carries a tenant_id).      Pla, require_doctor() (+7 more)

## Knowledge Gaps
- **1 isolated node(s):** `brain-api`
  These have ≤1 connection - possible missing edges or undocumented components.
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `get_settings()` connect `Community 37` to `Community 0`, `Community 2`, `Community 4`, `Community 6`, `Community 9`, `Community 10`, `Community 11`, `Community 12`, `Community 14`, `Community 15`, `Community 16`, `Community 17`, `Community 18`, `Community 19`, `Community 22`, `Community 23`, `Community 24`, `Community 26`, `Community 27`, `Community 28`, `Community 29`, `Community 34`, `Community 35`, `Community 36`, `Community 42`, `Community 44`, `Community 47`, `Community 50`, `Community 52`, `Community 53`, `Community 54`, `Community 58`, `Community 60`, `Community 62`, `Community 65`, `Community 68`, `Community 70`, `Community 71`, `Community 72`, `Community 74`, `Community 76`, `Community 78`, `Community 80`, `Community 82`, `Community 83`, `Community 86`, `Community 91`, `Community 92`, `Community 93`, `Community 101`?**
  _High betweenness centrality (0.166) - this node is a cross-community bridge._
- **Why does `Tenant` connect `Community 1` to `Community 2`, `Community 6`, `Community 7`, `Community 10`, `Community 12`, `Community 15`, `Community 16`, `Community 18`, `Community 19`, `Community 20`, `Community 22`, `Community 23`, `Community 25`, `Community 34`, `Community 43`, `Community 44`, `Community 45`, `Community 47`, `Community 51`, `Community 55`, `Community 56`, `Community 57`, `Community 59`, `Community 62`, `Community 64`, `Community 67`, `Community 74`, `Community 81`, `Community 83`, `Community 99`?**
  _High betweenness centrality (0.101) - this node is a cross-community bridge._
- **Why does `Entitlement` connect `Community 25` to `Community 4`, `Community 7`, `Community 10`, `Community 11`, `Community 14`, `Community 15`, `Community 16`, `Community 18`, `Community 20`, `Community 22`, `Community 34`, `Community 35`, `Community 42`, `Community 43`, `Community 44`, `Community 45`, `Community 55`, `Community 56`, `Community 57`, `Community 62`, `Community 64`, `Community 68`, `Community 81`?**
  _High betweenness centrality (0.090) - this node is a cross-community bridge._
- **Are the 64 inferred relationships involving `Tenant` (e.g. with `seed()` and `get_patient_invite()`) actually correct?**
  _`Tenant` has 64 INFERRED edges - model-reasoned connections that need verification._
- **Are the 97 inferred relationships involving `get_settings()` (e.g. with `seed_admin()` and `impersonate_token()`) actually correct?**
  _`get_settings()` has 97 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Alembic environment — async (asyncpg) configuration.`, `Run migrations in 'offline' mode (emit SQL, no live DB connection).`, `Run migrations in 'online' mode using an async engine.` to the rest of the system?**
  _904 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.036756756756756756 - nodes in this community are weakly interconnected._