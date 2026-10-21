# Graph Report - wt-api-sep  (2026-10-19)

## Corpus Check
- 302 files · ~347,168 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 4862 nodes · 11616 edges · 254 communities (241 shown, 13 thin omitted)
- Extraction: 83% EXTRACTED · 17% INFERRED · 0% AMBIGUOUS · INFERRED: 1929 edges (avg confidence: 0.77)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `4768a752`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- CheckoutSelection
- test_onboarding.py
- User
- _bearer
- test_admin_tenant_create.py
- test_precheck_handoff.py
- test_privacy.py
- test_launch_waitlist.py
- test_signup.py
- internal.py
- onboarding_sync.py
- billing.py
- select
- test_billing_phase1.py
- test_catalog_entitlements.py
- patient_access.py
- MessagePendingSession
- test_refresh_cookie.py
- BaseModel
- _bearer
- test_test_window.py
- apply_subscription_state
- create_checkout_session_for_intent
- test_portal_auto_greeting.py
- MessagePatient
- Entitlement
- admin.py
- auth.py
- _tenant_ids
- SlidingWindowLimiter
- test_precheck_billing.py
- test_secretary_role.py
- _FakeResponse
- create_demo_request_endpoint
- make_clinic
- internal.py
- make_sub
- precheck_client.py
- TokenResponse
- _families_from_subscription
- install_fake_settings
- select
- create_precheck_usage_event
- admin.py
- test_patient_account_name.py
- public_signup.py
- Any
- get_settings
- catalog.py
- test_doctor_secretaria.py
- test_patient_pending_session.py
- onboarding.py
- _request
- attachments.py
- open_pending
- HTTPException
- _tenant_com_precheck
- admin.py
- issue_password_reset_code
- _configure_mesh
- test_visit_retention.py
- test_smoke.py
- test_staff_first_access.py
- test_auth_hardening.py
- cookies.py
- secretaria_client.py
- test_professional_emails.py
- visit_retention.py
- test_mailer.py
- invite_existing
- Portal Messaging API — the Brain-Message channel
- CHECKPOINT — sessão pendente: o paciente conversa e agenda antes de provar o e-mail (2026-09-16)
- Guia: Obtenção e Configuração das Credenciais Meta para Embedded Signup
- cleanup_patients
- internal.py
- test_billing_checkout_guard.py
- _account_body
- internal.py
- Tenant
- Settings
- _request
- FakeStripe
- mask_email
- What changed, and where (brain-api)
- test_trial_por_plano.py
- env.py
- CHECKPOINT — Anexos no Portal (Brain-Message), parte 1: borda do brain-api
- 0020_patient_accounts.py
- 0013_secretary_role.py
- SecretariaResetIn
- billing.py
- CHECKPOINT — portal do paciente: login só por e-mail + clínica entra por convite (2026-09-15)
- test_billing_return_to.py
- test_doctor_anamneses_parity.py
- 0012_role_taxonomy.py
- 0014_password_reset.py
- 0015_precheck_provisioning.py
- CHECKPOINT — sessão de conta ativa pula pendente/e-mail/código ao abrir clínica nova (2026-09-24)
- CHECKPOINT — adicionar produto à assinatura (TASK C / TASK-015)
- config.py
- CHECKPOINT — PreCheck billing (plans, quota, top-up, tier upgrade)
- PendingIdentityIn
- CHECKPOINT — Test window (Task 2: reframe the free trial as a Meta/WABA acceptance window)
- __init__.py
- __init__.py
- CHECKPOINT — papel `secretary` (secretária humana da clínica)
- patient_access.py
- brain-api
- test_portal_enter_thread.py
- brain-api — API & Data Contracts (Phase 1)
- 6. Database tables (brain database)
- CHECKPOINT — Onboarding state machine, multi-professional & billing Phase 1
- test_billing_add_product.py
- MessagePatientSession
- CHECKPOINT — conta única multi-clínica do paciente (Brain-Message) (2026-09-12)
- CHECKPOINT — Launch waitlist (pre-launch buy gate, backend half)
- CHECKPOINT — Canal de entrega (whatsapp / brain_message) no modelo de dados
- CHECKPOINT — Meu Perfil (Part 1: self-edit foundation, `PATCH /doctor/me`)
- CHECKPOINT — reset de senha nativo do brain-api
- CHECKPOINT — as três faixas do PreCheck (50 / 100 / 300)
- Retenção de visitas vazias do Portal — TASK-042 (lado brain-api)
- CHECKPOINT — cupom de cortesia (acesso sem Stripe)
- CHECKPOINT — renovação silenciosa da sessão do paciente (Brain-Message) (2026-09-14)
- CHECKPOINT — contexto do agendamento no hand-off (nome + serviço)
- CHECKPOINT — Register at the first card (cold-signup login/entitlement split)
- Cross-database LGPD erasure & portability — design
- 2. Authentication
- CHECKPOINT — WhatsApp Coexistence onboarding (brain-api slice)
- CHECKPOINT — Refresh token em cookie httpOnly `__Host-` + guarda CSRF
- message_switchboard.py
- 13. Billing — Stripe (billing/auth round; `stripe-billing-entitlements`)
- 16. Onboarding state machine & multi-professional (onboarding round)
- Hub token — quem vê a agenda inteira (`agenda_scope`) Implementation Plan
- 10. Cross-product SSO — PreCheck handoff (implemented)
- CHECKPOINT — toque real no Portal: o campo atravessa o switchboard (2026-09-12)
- Mesh secret rotation — zero-downtime procedures
- _call
- TASK-055 — URLs públicas por serviço
- test_test_window_add_product.py
- 12. Doctor (tenant) API (RBAC round) — roles `doctor` / `manager` / `secretary`
- privacy.py
- CHECKPOINT — Hub token: quem vê a agenda inteira (`agenda_scope`) (TASK-044)
- CHECKPOINT — abertura do Portal: e-mail já cadastrado é detectado e volta mascarado (2026-09-21)
- 15. Cold signup — self-serve pay-and-provision (public, no human in the loop)
- 0021_patient_pending_sessions.py
- team.py
- Request
- 8. Errors and status codes
- CHECKPOINT — Estado da mensagem (enviado / entregue / lido / falhou), parte 2: brain-api
- conftest.py
- CHECKPOINT — a automação fala primeiro no Portal (perna brain-api da TASK-003)
- precheck_handoff
- Base
- upsert_waitlist_lead
- IdempotencyCache
- refresh_config_status
- test_config.py
- _upstream
- SignupIntent
- 7. Attachments / media — built end to end (2026-09-18; all three parts uncommitted, not deployed together)
- 0023_brain_message_default_on.py
- secretaria_internal.py
- apply_stripe_event
- _stripe_post
- PrivacyRequestIn
- __init__.py
- __init__.py
- __init__.py
- test_catalog_compose.py
- CHECKPOINT: clínica de teste criada pelo admin, sem Stripe (`POST /admin/tenants`)
- usage_summary
- CHECKPOINT — cobertura das pontes de provisionamento (secretarIA / PreCheck)
- CONTRACT_doctor_anamneses.md
- setup_response
- 1. Authentication
- test_hub_agenda_scope.py
- PrecheckAccountLink
- resolve_agenda_scope
- DemoRequest
- DoctorMeOut
- IntakeIn
- brain-api — instruções para Codex
- redeem
- test_stripe_proof_guard.py
- decode_token
- test_migration_0026_entitlement_product_state.py
- test_auth_email_after_response.py
- _send_precheck_attachment
- resolve_entitlement
- CHECKPOINT — e-mails de autenticação no brain-api + "esqueci a senha" por código (TASK-056)
- join_launch_waitlist
- test_billing_stripe_fixtures.py
- TASK-058 — Restauro do vínculo clínico (fase 2)
- stripe_proof_guard.py
- AdminTenantCreateIn
- record_usage
- invite_codes.py
- Checkpoint — digitando backend (TASK-033)
- Conta pessoal de equipe — contrato congelado TASK-057
- team_auto_email_acceptance.py
- test_password_reset.py
- EntitlementPatchIn
- test_patient_visit_merge.py
- require_internal_api_key
- request_pending_otp
- PrivacyRequest
- entitlement.py
- test_staff_account_sessions.py
- test_message_channels.py
- Limpeza administrativa de pacientes por clínica — TASK-029
- claim_pending_email
- demo.py
- signup.py
- 0031_staff_accounts.py
- 0032_staff_membership_email.py
- TASK-057 — conta pessoal de equipe e vínculos clínicos
- main
- AttachmentRefused
- available_products
- create_admin
- mark_thread_read
- health.py
- request_pending_otp_internal
- AdminTenantOut
- PrecheckSsoTokenResponse
- test_window_email_due

## God Nodes (most connected - your core abstractions)
1. `_bearer()` - 215 edges
2. `_token()` - 203 edges
3. `Tenant` - 164 edges
4. `get_settings()` - 162 edges
5. `MessagePatient` - 124 edges
6. `User` - 116 edges
7. `Entitlement` - 106 edges
8. `_configure_mesh()` - 102 edges
9. `_bearer()` - 94 edges
10. `select()` - 92 edges

## Surprising Connections (you probably didn't know these)
- `staff_accounts_enabled()` --calls--> `get_settings()`  [INFERRED]
  tests/conftest.py → src/brain_api/config.py
- `test_login_returns_refresh_and_expiry()` --calls--> `get_settings()`  [INFERRED]
  tests/test_auth_hardening.py → src/brain_api/config.py
- `smtp_on()` --calls--> `get_settings()`  [INFERRED]
  tests/test_mailer.py → src/brain_api/config.py
- `test_legacy_invite_writer_uses_restricted_first_access_when_enabled()` --calls--> `get_settings()`  [INFERRED]
  tests/test_staff_account_sessions.py → src/brain_api/config.py
- `test_delivery_status_uses_generation_and_active_access()` --calls--> `get_settings()`  [INFERRED]
  tests/test_team_delivery.py → src/brain_api/config.py

## Import Cycles
- None detected.

## Communities (254 total, 13 thin omitted)

### Community 0 - "CheckoutSelection"
Cohesion: 0.10
Nodes (28): _append_checkout_line_items(), _append_subscription_items(), _append_subscription_selections(), _apply_setup_custom_text(), _apply_trial(), CheckoutSelection, create_checkout_session(), _create_subscription_for_signup() (+20 more)

### Community 1 - "test_onboarding.py"
Cohesion: 0.05
Nodes (29): _new_tenant(), AsyncSession, services/onboarding.py — pure state-machine logic (STATE-MACHINE + AUTH build)., Both hint groups present -> the numero_em_outro_bsp check wins (checked first)., `record_attempt` never commits — a rollback after it must undo everything., prior_api=='yes' takes priority even when fb_page ALSO signals a blocker., test_apply_config_status_complete_but_not_connected(), test_apply_config_status_connected_and_complete() (+21 more)

### Community 2 - "User"
Cohesion: 0.07
Nodes (61): async_sessionmaker, AsyncClient, AsyncSession, Create the platform admin if it does not already exist. Idempotent.      Crede, seed_admin(), hash_password(), hash_refresh_token(), SHA-256 hex for storage/lookup. Fast is fine: the input is 64 random bytes (+53 more)

### Community 3 - "_bearer"
Cohesion: 0.03
Nodes (141): Controle: a trava só pega `is_test`. Uma clínica real segue o caminho normal (aq, test_clinica_real_nao_e_barrada_pela_trava(), Tenant cascade-delete tests (DELETE /admin/tenants/{id}).  Two layers: - Endp, The delete route is behind the same admin gate as the rest of /admin/*., A deleted clinic vanishes from every admin view and its owner can no longer log, test_delete_tenant_removes_clinic_everywhere(), test_delete_tenant_requires_admin(), test_delete_tenant_unknown_is_404() (+133 more)

### Community 4 - "test_admin_tenant_create.py"
Cohesion: 0.05
Nodes (84): BoundLogger, _descreve(), main(), Cria (ou atualiza) um cupom de cortesia — acesso liberado sem passar pelo Stripe, get_logger(), Structured logging setup using structlog.  JSON output in non-dev environments, Blank any value whose key ends in `_encrypted` or looks secret-bearing., Configure structlog + stdlib logging. Safe to call more than once. (+76 more)

### Community 5 - "test_precheck_handoff.py"
Cohesion: 0.07
Nodes (88): _body(), _body_ctx(), _body_portal(), _entitled_tenant(), _FakeResponse, _install_fake_httpx(), _patch_entitlement(), Exception (+80 more)

### Community 6 - "test_privacy.py"
Cohesion: 0.19
Nodes (24): _admin_token(), _FakeResponse, _install_fake_httpx(), _privacy_requests(), Exception, MonkeyPatch, LGPD cross-database privacy orchestration tests (CONTRACTS.md §14).  Ground tr, Point services/privacy at a configured mesh + a fake httpx routed by base_url. (+16 more)

### Community 7 - "test_launch_waitlist.py"
Cohesion: 0.13
Nodes (19): _db(), POST /public/launch-waitlist — the pre-launch buy gate's lead capture.  Covers, A later hint-less click must not erase the plan we already know they wanted., A filled honeypot returns 201 but persists nothing., Isolated lead capture (CONTRACTS.md §0.4): no tenant, no entitlement, no billing, A session on the SAME in-memory DB the `client` fixture serves from., Every captured lead, oldest first., The whole point: three plan clicks by one visitor leave ONE row, same id. (+11 more)

### Community 8 - "test_signup.py"
Cohesion: 0.05
Nodes (58): _checkout(), The authenticated checkout follows the trial policy of `_trial_days_for` (TASK C, test_a_precheck_checkout_carries_no_trial_even_when_a_trial_is_configured(), test_a_secretaria_checkout_still_carries_the_trial(), _install_fake_stripe_httpx(), Point services.billing at fake Stripe settings + a recording fake httpx client., _force_intent_pending(), Cold-signup vertical tests: register-at-first-card, Checkout Session, onboarding (+50 more)

### Community 9 - "internal.py"
Cohesion: 0.14
Nodes (14): secretarIA -> brain-api: the patient answered "qual é o seu nome?" at this clini, record_patient_name(), PatientContactOut, PatientNameIn, PatientNameOut, PendingIdentityStatusOut, PendingOtpCancelOut, PendingOtpVerifyIn (+6 more)

### Community 10 - "onboarding_sync.py"
Cohesion: 0.15
Nodes (23): ensure_precheck_provisioned(), ensure_products_provisioned(), ensure_secretaria_provisioned(), get_cached_config_status(), get_last_attempt(), get_owner(), invalidate_config_status(), list_onboarding_tenants() (+15 more)

### Community 11 - "billing.py"
Cohesion: 0.07
Nodes (39): add_product(), checkout(), portal(), precheck_topup(), precheck_upgrade(), precheck_usage(), _precheck_usage_out(), AsyncSession (+31 more)

### Community 12 - "select"
Cohesion: 0.08
Nodes (54): Network acceptance against team_local_server; only boolean sanitized evidence., select(), _peek_code(), `POST /refresh` as the portal sends it. With `cookie`, that exact value is sent, Brute-force the code out of its HASH — the test's stand-in for reading the e-mai, Past `expires_at` the code is dead even though it is the RIGHT code., The second presentation of a correct code fails — `consumed_at` is burned., After PATIENT_OTP_MAX_ATTEMPTS wrong guesses the RIGHT code stops working. (+46 more)

### Community 13 - "test_billing_phase1.py"
Cohesion: 0.08
Nodes (46): _extended_fake_settings(), _link_stripe_customer(), SimpleNamespace, Billing Phase 1 / fully-metered vertical tests (CONTRACT_onboarding_v1.md §9): t, Same forwarding mechanics as billable_patients, dispatched to the DIFFERENT, Without a `{plan}_metered` STRIPE_PRICE_MAP key and with STRIPE_TRIAL_PERIOD_DAY, Same forwarding mechanics as billable_patients/active_professionals, dispatched, The cold-signup checkout builder no longer carries ANY of the fully-metered shap (+38 more)

### Community 14 - "test_catalog_entitlements.py"
Cohesion: 0.06
Nodes (33): Protocol, check_quota(), Decision, EntitlementLike, is_entitled(), UUID, Entitlement resolution service (stripe-billing-entitlements skill, CONTRACTS.md, Outcome of a pure, in-process quota check (mirrors the skill).      Scaffold f (+25 more)

### Community 15 - "patient_access.py"
Cohesion: 0.06
Nodes (34): list_threads(), lookup_clinic(), patient_typing(), The pre-login half of `GET /entitlements`, narrowed to what a visitor may know., Exactly `EntitlementOut.products` intersected with `channels.brain_message`., ClinicInviteIn, ClinicLookupIn, ClinicPublicOut (+26 more)

### Community 16 - "MessagePendingSession"
Cohesion: 0.10
Nodes (29): MultiPartException, _BodyTooLarge, pending_status(), The typed state the Portal uses for its composer; no copy matching required., Raised from inside the body stream when a request outgrows its cap.      A `Mu, MessagePendingSession, A visitor who is TALKING but has proven nothing yet (2026-09-16).      THE PRO, adopt_pending_identity() (+21 more)

### Community 17 - "test_refresh_cookie.py"
Cohesion: 0.10
Nodes (42): _cookie_value(), _login(), The HttpOnly refresh cookie: emission, the cookie-first refresh, the CSRF header, A client presenting both is one caught mid-deploy; the cookie holds the leg, The un-migrated portal must keep working, header-less, for the whole round., 401, not the 422 an unconditionally-required body would give: 'no credential', A cross-site form POST carries the cookie but cannot set a header. This check, The guard must fire BEFORE rotation — otherwise a forged request still burns (+34 more)

### Community 18 - "BaseModel"
Cohesion: 0.05
Nodes (58): BaseModel, ExchangeInviteTokenIn, ExchangeOnboardingTokenIn, LoginRequest, LogoutRequest, MeResponse, MessageOut, PasswordResetCodeVerifyIn (+50 more)

### Community 19 - "_bearer"
Cohesion: 0.05
Nodes (97): _bearer(), _confirm(), _confirm_url(), _link_events(), _login(), UUID, Patient access by e-mail OTP + the product switchboard (Brain-Message, piece 7)., The address proven at BOTH open clinics, with the login at `both` LAST.      T (+89 more)

### Community 20 - "test_test_window.py"
Cohesion: 0.16
Nodes (29): _due_entitlement(), _due_tenant(), _noop_async(), SimpleNamespace, Task 2 — reframe the free trial as a Meta/WABA acceptance test-window (backend)., Task 2's core billing-semantics change: the billing cycle anchors at 'conectado', The pre-existing tenant A fixture (plan=precheck, status=active, no subscription, The core due condition deliberately does NOT require ent.status active/trialing (+21 more)

### Community 21 - "apply_subscription_state"
Cohesion: 0.23
Nodes (20): apply_subscription_state(), Apply ONE subscription object (webhook event, or the update response of     `ad, _resolve_status(), _clinic(), apply_subscription_state: previous row x subscription event -> next row (TASK C,, test_a_family_removed_from_the_subscription_turns_off(), test_a_live_subscription_converts_the_manual_family_it_carries(), test_addons_and_quantity_come_from_the_live_subscription() (+12 more)

### Community 22 - "create_checkout_session_for_intent"
Cohesion: 0.20
Nodes (14): attach_intake(), create_checkout_session_for_intent(), _normalize_catalog_ids(), _plan_id_of(), provision_tenant_from_intent(), AsyncSession, UUID, Store the pre-checkout eligibility answers on the tenant's pending signup intent (+6 more)

### Community 23 - "test_portal_auto_greeting.py"
Cohesion: 0.06
Nodes (50): _account_token(), _boom_on_open(), _invite(), _open(), _open_calls(), Exception, The automation speaks FIRST (TASK-003 §2) — `POST {SECRETARIA}/internal/brain-me, F5. The cookie resumes the visit, and a resumed visit is not a new patient. (+42 more)

### Community 24 - "MessagePatient"
Cohesion: 0.12
Nodes (44): MessagePatient, One patient's stable identity on the Brain-Message channel of ONE clinic., _count(), _ids(), maker(), UUID, Just enough of `httpx.Response` for `message_switchboard.discard_empty_visit`., Drive the REAL discard client: record the HTTP hop, answer whatever the test say (+36 more)

### Community 25 - "Entitlement"
Cohesion: 0.07
Nodes (38): Entitlement, Entitlement model — one row per tenant (stripe-billing-entitlements skill).  T, Per-tenant entitlement state. Primary-keyed by tenant_id (one row per tenant)., _manual_precheck_plan(), _manual_secretaria_plan(), _live_subscription(), A fake `_stripe_get` returning a live subscription snapshot (FIX 1b)., Schedules using the LIVE trial_end (from `_stripe_get`), NOT the event's own -- (+30 more)

### Community 26 - "admin.py"
Cohesion: 0.09
Nodes (31): delete_tenant(), get_admin_anamnesis(), get_admin_metrics(), get_secretaria_tenants(), get_tenant(), get_tenant_entitlements(), impersonate_token(), list_admin_anamneses() (+23 more)

### Community 27 - "auth.py"
Cohesion: 0.13
Nodes (35): build_session_response(), _check_auth_rate_limit(), exchange_invite_token(), exchange_onboarding_token(), _expired_cookie_headers(), issue_session(), login(), logout() (+27 more)

### Community 28 - "_tenant_ids"
Cohesion: 0.06
Nodes (77): Shared fakes for the TASK C billing tests (price map, settings, subscription obj, (started_at, notified_at) as timezone-aware UTC (SQLite hands back naive values), read_window(), set_window(), _admin_entitlements(), MANDATORY regression (spec C §5.14): checkout -> completed -> created -> updated, test_courtesy_secretaria_survives_buying_precheck_through_every_webhook(), _event() (+69 more)

### Community 29 - "SlidingWindowLimiter"
Cohesion: 0.14
Nodes (11): In-process sliding-window rate limiter (CONTRACTS.md §5).  Deliberately trivia, Per-key (client IP) sliding 60s window. `limit_getter` is read per call so a, True if `key` is under the limit (and records the hit), else False.          F, SlidingWindowLimiter, test_login_rate_limited(), Second call from the same IP inside the window -> 429 (limit forced to 1)., test_rate_limit_trips(), test_per_address_request_limit() (+3 more)

### Community 30 - "test_precheck_billing.py"
Cohesion: 0.08
Nodes (30): PreCheck billing vertical tests (precheck-billing round).  Ground truth: servi, PRECHECK_API_KEY is unset by default (conftest never sets it) -> fail closed., precheck_consultations is flat+quota billing, never metered — record_usage must, The chosen quantity drives BOTH the line item (so Stripe charges quantity x unit, Below the minimum is refused SERVER-SIDE, whatever the frontend allows -- and, The minimum itself is INSIDE the allowed range (boundary, not an off-by-one)., Clinic A is seeded with plan="precheck" -- the LEGACY alias for precheck_basic -, Clinic A's seeded entitlement carries no stripe_subscription_id at all. (+22 more)

### Community 31 - "test_secretary_role.py"
Cohesion: 0.11
Nodes (31): _bearer(), _noop_async(), `secretary` role tests (secretary round, 2026-08-14).  The role is defined as, The constant is wired into the canonical tuple, not just defined next to it., A secretary authenticates like anyone else; the claim survives into the JWT., Every day-to-day surface answers a secretary token: profile, the clinic's agenda, `require_owner` accepts `secretary` as an ALTERNATIVE to `is_owner` — without, The invite mirrors the professional one EXCEPT for the secretaria round-trip: no (+23 more)

### Community 32 - "_FakeResponse"
Cohesion: 0.12
Nodes (30): _FakeResponse, _install_fake_meta_httpx(), _install_fake_provisioning_httpx(), _install_fake_waba_subscribe_httpx(), Exception, MonkeyPatch, SimpleNamespace, (b) subscribe_app_to_waba success — hits the right path, Bearer header only, no (+22 more)

### Community 33 - "create_demo_request_endpoint"
Cohesion: 0.11
Nodes (16): create_demo_request_endpoint(), AsyncSession, Request, Public demo-request endpoint (CONTRACTS.md §4 + §5): "Agendar demo" lead capture, Capture a demo request: honeypot + rate-limit guard, then persist + confirm., DemoRequestConfirmation, DemoRequestCreate, Body for `POST /demo-requests` (CONTRACTS.md §4.1).      Both enum fields are (+8 more)

### Community 34 - "make_clinic"
Cohesion: 0.18
Nodes (26): make_clinic(), A tenant + its entitlement, composed per product family (no HTTP, no bridges)., Preflight of add_product_to_subscription: every refusal happens BEFORE any write, Courtesy secretarIA + a paid PreCheck subscription: adding secretarIA to it is a, _refusal(), _req(), test_a_family_the_live_subscription_already_carries_is_already_present(), test_a_manual_product_may_be_bought_and_converted_to_paid() (+18 more)

### Community 35 - "internal.py"
Cohesion: 0.20
Nodes (18): create_usage_event(), internal_entitlements(), internal_professional_emails(), post_onboarding_event(), AsyncSession, description, Path, Response (+10 more)

### Community 36 - "make_sub"
Cohesion: 0.13
Nodes (28): Families this subscription pays for (the combo pays for both)., make_sub(), A Stripe subscription object as the webhook / `GET /v1/subscriptions/{id}` deliv, invoice(), Final review regressions: financial consent, independent writers and manual fami, test_bad_quote_never_mutates_stripe(), test_changed_expected_charge_refuses_before_update(), test_complex_or_partial_invoice_fails_closed() (+20 more)

### Community 37 - "precheck_client.py"
Cohesion: 0.13
Nodes (27): _empty_page(), get_admin_anamnesis(), get_admin_metrics(), get_anamnesis(), get_anamnesis_media_url(), list_admin_anamneses(), list_anamneses(), list_anamnesis_media() (+19 more)

### Community 38 - "TokenResponse"
Cohesion: 0.07
Nodes (34): checkout_config(), No rate limit — deliberate. Unlike the routes above, this touches no DB and, The brain-api session pair. Shape mirrors PreCheck's `TokenResponse` and the, TokenResponse, AddonAvailabilityOut, CheckoutConfigOut, CheckoutSessionCreate, CheckoutSessionOut (+26 more)

### Community 39 - "_families_from_subscription"
Cohesion: 0.14
Nodes (14): catalog_id_for_price(), _compose_with_addons(), _families_from_subscription(), _plan_id_from_metered_companion(), If `cid` is a `{plan_id}_metered_patients` / `{plan_id}_metered_professionals` /, The product families ONE Stripe subscription carries, derived from its items., Map a subscription's items to the families they carry; None when NO plan is reco, Quantity scaling: `compose_entitlement_state` grants each active add-on once; ad (+6 more)

### Community 40 - "install_fake_settings"
Cohesion: 0.12
Nodes (25): classify_add_product_error(), A Stripe API refusal that KEEPS Stripe's own error fields (HTTP status, `error.c, `_stripe_post` that raises `StripeApiError` (with Stripe's error fields) on a >=, Map a Stripe refusal of the subscription update to `(http_status, detail)`., _stripe_post_or_raise(), StripeApiError, fake_settings(), install_fake_settings() (+17 more)

### Community 41 - "select"
Cohesion: 0.10
Nodes (54): select(), verify_password(), Personal staff credentials; clinical authority remains on User, never here., StaffAccount, StaffAccountResetCode, StaffAccountSession, create_tenant(), create_user() (+46 more)

### Community 42 - "create_precheck_usage_event"
Cohesion: 0.11
Nodes (20): _precheck_key_scheme, create_precheck_usage_event(), get_precheck_quota(), AsyncSession, description, Path, Security, UUID (+12 more)

### Community 43 - "admin.py"
Cohesion: 0.14
Nodes (25): EntitlementAdminOut, Full entitlement record for one tenant (admin read).      The Stripe linkage i, _bulk_delete(), _delete_secretaria_tenant(), delete_tenant(), _entitlement_out(), get_entitlement(), get_tenant_detail() (+17 more)

### Community 44 - "test_patient_account_name.py"
Cohesion: 0.08
Nodes (65): _handle_at(), _name_of(), _opens(), UUID, The patient's NAME follows the account to its next clinic (2026-09-24).  A liv, Named at clinic A; clinic B's link with the account cookie -> `open` carries the, Nothing invented: secretarIA gets `null` and asks once., Regression: without the account cookie the `open` is today's, name-free. (+57 more)

### Community 45 - "public_signup.py"
Cohesion: 0.19
Nodes (18): _check_rate_limit(), create_checkout_session(), onboarding_status(), AsyncSession, CheckoutSessionOut, Request, Response, UUID (+10 more)

### Community 46 - "Any"
Cohesion: 0.12
Nodes (25): AddProductCharge, _charge_from_update(), _is_timestamp(), _iso_date(), _line_is_proration(), parse_preview_charge(), _period_dt(), period_end_of() (+17 more)

### Community 47 - "get_settings"
Cohesion: 0.07
Nodes (47): list_onboarding_tenants(), secretaria's `run_onboarding_nudges` cron pulls this hourly to decide who needs, _action_out(), attach_intake(), _embedded_signup_out(), get_onboarding(), get_test_window(), invite_professional() (+39 more)

### Community 48 - "catalog.py"
Cohesion: 0.11
Nodes (27): AddonDef, compose_entitlement_state(), compute_entitlement_state(), compute_limits(), default_addons(), families_enabled(), get_addon(), get_plan() (+19 more)

### Community 49 - "test_doctor_secretaria.py"
Cohesion: 0.21
Nodes (17): _FakeResponse, _install_fake_httpx(), Exception, MonkeyPatch, brain-api -> secretaria internal data path (/doctor/appointments + /doctor/patie, secretaria's 401 (key mismatch) becomes a clean 502 — body never leaks to the do, secretaria's 403 (its OWN key unset) is an unconfigured mesh => empty page, not, Point the client at a configured mesh and a fake httpx that records the call. (+9 more)

### Community 50 - "test_patient_pending_session.py"
Cohesion: 0.05
Nodes (85): _claim(), _count(), _invite(), _open(), _prove(), The PENDING visit (2026-09-16): the patient talks — and books — before proving a, secretarIA's leg: the address the patient typed into the chat., The field is non-optional on a 200, and this is why that is honest: a visit with (+77 more)

### Community 51 - "onboarding.py"
Cohesion: 0.16
Nodes (16): apply_config_status(), derive_initial_state(), initial_next_retry_at(), map_error_to_blocker(), provision_defaults(), AsyncSession, datetime, UUID (+8 more)

### Community 52 - "_request"
Cohesion: 0.18
Nodes (20): activate_tenant(), connect_whatsapp(), create_professional(), get_config_status(), get_professionals(), provision_tenant(), Any, Response (+12 more)

### Community 53 - "attachments.py"
Cohesion: 0.18
Nodes (17): AttachmentKind, check_attachment(), _clean(), extension_conflicts(), is_media_id(), Attachments on the Brain-Message channel — the ONE place their limits and checks, The kind the CONTENT says it is, from its magic bytes — or None.      Never th, The last path segment of `raw`, without what is unsafe in a header, a path or a (+9 more)

### Community 54 - "open_pending"
Cohesion: 0.12
Nodes (28): add_clinic_by_invite(), _clinic_session(), confirm_sibling(), _greet_if_secretaria(), _open_if_precheck(), open_pending(), _open_with_account(), _public_products() (+20 more)

### Community 55 - "HTTPException"
Cohesion: 0.05
Nodes (83): HTTPException, Proxy secretaria's destructive data wipe (brain-api -> secretaria `/admin/reset`, reset_secretaria(), Billing endpoints (stripe-billing-entitlements skill).  Authenticated tenant a, `require_tenant`, plus: a test clinic (`Tenant.is_test`, created by `POST     /, `require_billable_tenant` (tenant in context + not a test clinic — that 403 come, require_billable_tenant(), require_billing_manager() (+75 more)

### Community 56 - "_tenant_com_precheck"
Cohesion: 0.19
Nodes (16): chamadas(), Tests de `onboarding_sync.ensure_precheck_provisioned` — o bridge que faz "pago, A frase é o que o paciente vê no QR impresso na recepção e o que digita     qua, Um nome que não sobrevive à normalização não pode virar "precheck " solto —, Captura as chamadas ao PreCheck e deixa o teste escolher a resposta., _tenant_com_precheck(), test_excecao_inesperada_e_engolida(), test_falha_do_precheck_nao_levanta_e_nao_carimba() (+8 more)

### Community 57 - "admin.py"
Cohesion: 0.12
Nodes (22): create_user(), Create a user in any tenant with any role (how we onboard doctor accounts)., AdminDemoRequestOut, AdminDemoRequestPatchIn, AdminTenantDeleteOut, AdminTenantDetailOut, AdminUserCreateIn, AdminUserOut (+14 more)

### Community 58 - "issue_password_reset_code"
Cohesion: 0.07
Nodes (48): get_current_principal(), AsyncSession, Mutable clinic access is checked on every request, including old bearer tokens., StaffPasswordResetCode — the pending e-mailed reset code for one staff user., StaffPasswordResetCode, _as_utc(), get_tenant(), get_user() (+40 more)

### Community 59 - "_configure_mesh"
Cohesion: 0.05
Nodes (104): _configure_mesh(), Point both upstreams somewhere and give each its OWN secret.      Two DIFFEREN, _mesh(), _patient(), _png(), _precheck_turn(), _queued(), Attachments on the Brain-Message channel — the brain-api edge (2026-09-18).  P (+96 more)

### Community 60 - "test_visit_retention.py"
Cohesion: 0.13
Nodes (43): MockTransport, _count(), maker(), UUID, TASK-042: empty Portal visits are deleted after 24 h; a typed e-mail never is., secretarIA deleted but its answer was lost: the next `absent` is permission (I2), PreCheck's automatic greeting does not count: no patient message -> cleaned (own, Typed in PreCheck (or anywhere): kept, at any clinic, without even asking secret (+35 more)

### Community 61 - "test_smoke.py"
Cohesion: 0.06
Nodes (28): _admin_token(), _decode_like_precheck(), End-to-end smoke test of the full HTTP contract (CONTRACTS.md).  Runs the real, A valid tenant with no entitlement row gets coherent defaults, not a 404., A filled honeypot returns 201 but persists nothing., The real seeded administrator; signatures do not replace live user state., Validate the token EXACTLY as PreCheck does — not a mock.      Mirrors PreChec, Entitled + linked brain user -> a token PreCheck's own auth accepts. (+20 more)

### Community 62 - "test_staff_first_access.py"
Cohesion: 0.07
Nodes (29): decode_staff_token(), Purpose-separated keys: permissive shared-key consumers cannot accept staff proo, staff_token_key(), aware(), complete_staff_first_access(), create_staff_setup_token(), AsyncSession, datetime (+21 more)

### Community 63 - "test_auth_hardening.py"
Cohesion: 0.09
Nodes (25): create_hub_token(), decode_hub_token(), Mint the tenant-scoped token the doctor portal presents to secretarIA's hub., Validate a hub token: signature/expiry via `decode_token` + the exact scope., Auth-hardening round tests: refresh rotation/reuse, rate limiting, password poli, SECRETARIA_API_KEY is forced empty by conftest -> fail closed with 403., _tenant_ids(), test_auth_me_exposes_professional_id_when_present() (+17 more)

### Community 64 - "cookies.py"
Cohesion: 0.12
Nodes (34): accept_invite(), clear_cookie(), complete_first_access(), current_account(), login(), logout(), memberships(), preference() (+26 more)

### Community 65 - "secretaria_client.py"
Cohesion: 0.21
Nodes (14): _admin_request(), delete_tenant(), list_tenants(), Any, Response, UUID, Service-to-service admin client into secretaria (the cross-API admin connection), Extract a safe `detail` string from an upstream error response. (+6 more)

### Community 66 - "test_professional_emails.py"
Cohesion: 0.22
Nodes (14): Tests for `GET /internal/tenants/{tenant_id}/professional-emails`.  The read t, A tenant with nobody linked and a tenant that does not exist answer the     sam, Asking about tenant B never returns tenant A's address.      The isolation inv, SECRETARIA_API_KEY is forced empty by conftest -> fail closed with 403.      A, Tenant B's owner has no `professional_id`.      Absent, not present-with-null:, _set_pair_key(), _tenant_ids(), test_linked_professional_is_returned() (+6 more)

### Community 67 - "visit_retention.py"
Cohesion: 0.20
Nodes (20): _delete_brain_side(), _empty_visit_filter(), EmptyVisit, find_empty_visits(), _precheck_clinics(), AsyncSession, datetime, UUID (+12 more)

### Community 68 - "test_mailer.py"
Cohesion: 0.07
Nodes (18): EmailTemplate, _header(), is_configured(), Outbound authentication e-mail — brain-api's own SMTP sender.  Until TASK-056, One line, no CR/LF: header values carry clinic-supplied text (clinic_name)., (subject, body). Raises KeyError for an unknown template or a missing variable., Blocking send — always called through `asyncio.to_thread`., Render and send one authentication e-mail. NEVER raises. (+10 more)

### Community 69 - "invite_existing"
Cohesion: 0.23
Nodes (11): StaffMembershipInvite, TeamInviteInput, accept_invitation(), deliver_invitation(), invite_existing(), AsyncSession, Target-managed invitations. Proof of the personal account is required to accept., resend_invitation() (+3 more)

### Community 70 - "Portal Messaging API — the Brain-Message channel"
Cohesion: 0.10
Nodes (20): 0. Topology, 10. What this is NOT, 2.1 → secretarIA (async, 202), 2.2 → PreCheck (sync, 200), 2. Send — patient → clinic, 3.1 secretarIA — read/write, 3.2 PreCheck — read-only, by design, 3. Send — clinic → patient (+12 more)

### Community 71 - "CHECKPOINT — sessão pendente: o paciente conversa e agenda antes de provar o e-mail (2026-09-16)"
Cohesion: 0.09
Nodes (22): §10 — Ordem de deploy, §11 — Provado em produção (2026-09-16, sessão de QA separada da implementação), §12 — Emenda para OTP inline da secretarIA (onda 2, 2026-09-17), §1 — O que o dono fechou (não reabrir), §2 — Causa raiz: nada disso existia, §3 — Modelo escolhido: tabela própria para a VISITA, identidade sem endereço, §4 — O token da sessão pendente (formato exato), 5.1 `POST /patient-access/clinics/lookup` — o que a clínica oferece, ANTES de qualquer login (+14 more)

### Community 72 - "Guia: Obtenção e Configuração das Credenciais Meta para Embedded Signup"
Cohesion: 0.09
Nodes (22): 1. App ID e App Secret, 1. Configuração do brain-api, 2. Configuration ID do Embedded Signup (caminho v4 — ver seção "Versão do Embedded Signup"), 3. Webhook fields e verify token (necessário para o fluxo de Coexistence), brain-frontend: Nenhuma Configuração Necessária, Coexistence — Task 0: grafia do feature type (pesquisa fechada, rodada "WhatsApp Coexistence onboarding"), Conclusão: são três eixos independentes, Critério 1: backend respondendo `configured=true` (+14 more)

### Community 73 - "cleanup_patients"
Cohesion: 0.22
Nodes (14): execute(), preview(), AsyncSession, UUID, Platform-admin-only preview and execution of patient cleanup for ONE clinic., CleanupResult, ProductResult, Swagger contract for one-clinic patient cleanup; no patient data in responses. (+6 more)

### Community 74 - "internal.py"
Cohesion: 0.22
Nodes (12): cancel_pending_otp_internal(), pending_identity_status(), AsyncSession, brain-api's INBOUND service-to-service API for the Portal (Brain-Message channel, Tell secretarIA whether this exact clinic handle still needs an address., Called when the chat abandons the code wait to ask for a different address inste, Prove the address without attempting to mint a browser credential on this leg., `verified`, plus the proven address's account name when it has one.      Only (+4 more)

### Community 75 - "test_billing_checkout_guard.py"
Cohesion: 0.16
Nodes (23): checkout_block_reason(), Why a NEW subscription checkout must not be opened for this tenant, or None., Write entitlement columns straight to the DB (no bridges, no webhook): the state, set_entitlement(), _checkout(), _ent(), Guard on POST /billing/checkout (TASK B introduced it; TASK C narrowed it — spec, TASK B's rule 2 is gone: a courtesy/admin secretarIA clinic may open a checkout. (+15 more)

### Community 76 - "_account_body"
Cohesion: 0.13
Nodes (23): _account_body(), complete_pending(), _expired_patient_cookie_headers(), Response, The only leg allowed to promote the browser after secretarIA verified its code., Burn the code, open the account, and KEEP THE HANDLE this conversation was born, The `Set-Cookie` that deletes the patient cookie, as raise-able headers., `verify-otp`'s and `refresh`'s body: the account token and one token per clinic. (+15 more)

### Community 77 - "internal.py"
Cohesion: 0.10
Nodes (19): HubTokenVerifyIn, HubTokenVerifyOut, InternalEntitlementOut, InternalOnboardingEventOut, InternalOnboardingListOut, InternalOnboardingTenantOut, InternalProfessionalEmailOut, InternalProfessionalEmailsOut (+11 more)

### Community 78 - "Tenant"
Cohesion: 0.06
Nodes (43): create_patient_account_token(), create_patient_pending_token(), Mint the short-lived token of one patient ACCOUNT login.      `sub` is `messag, Mint the short-lived token of one PENDING visit.      `sub` is the `MessagePat, Tenant model — one clinic / organization on the Brain platform.  Non-sensitive, A clinic/organization. Owns users and a single entitlements row., Tenant, Usage-event ledger — one row per recorded internal usage event (metering).  Pe (+35 more)

### Community 79 - "Settings"
Cohesion: 0.12
Nodes (11): BaseSettings, Strongly-typed settings. Values come from the environment or `.env`.      Real, Parse CORS_ALLOW_ORIGINS into a clean list of origins.          Accepts a JSON, Settings, invite_link(), The link a clinic hands out, or `None` while `BRAIN_MESSAGE_PUBLIC_URL` is unset, test_the_launch_gate_defaults_to_off(), Public links share one configured base per frontend. (+3 more)

### Community 80 - "_request"
Cohesion: 0.31
Nodes (8): provision_clinic(), Any, Response, UUID, Internal service-to-service PROVISIONING client into PreCheck (`X-Internal-Token, Call `method path` on PreCheck's internal surface with the shared pair key., `POST /internal/provision` — create (or idempotently confirm) the PreCheck clini, _request()

### Community 81 - "FakeStripe"
Cohesion: 0.19
Nodes (23): FakeStripe, Every billing-relevant column + the test window (what a wrong write would change, Records every Stripe call of the add-product flow and answers from canned object, snapshot(), _post(), _precheck_clinic(), POST /billing/add-product over HTTP (TASK C, spec §5.5): contract, gates, error, test_a_card_declined_is_402_and_leaves_the_row_untouched() (+15 more)

### Community 82 - "mask_email"
Cohesion: 0.12
Nodes (22): _clusters(), mask_email(), The ONE shape a patient's e-mail address is allowed to leave this service in., `text` split into base-plus-combining-marks groups — a poor man's grapheme clust, `ana@gmail.com` -> `a***a@gmail.com`; `a@gmail.com` -> `a***@gmail.com`., `core/email_mask.py` — the only shape of a patient's address allowed to leave (T, `unicodedata.normalize` tolerates it here; the point is that nothing explodes., Total function: anything that is not a usable address collapses to `***`. (+14 more)

### Community 83 - "What changed, and where (brain-api)"
Cohesion: 0.11
Nodes (18): 10. `services/precheck_client.py`, 11. `migrations/versions/0012_role_taxonomy.py` (revises `0011_launch_waitlist`), 1. `models/user.py`, 2. `core/security.py` — `create_access_token`, 3. `api/deps.py`, 4. `schemas/admin.py`, 5. `services/admin.py`, 6. `services/signup.py` (+10 more)

### Community 84 - "test_trial_por_plano.py"
Cohesion: 0.36
Nodes (8): O trial é por PLANO, não global (`billing._trial_days_for`).  `STRIPE_TRIAL_PE, A página de pagamento do PreCheck não pode falar de aprovação da Meta., _sel(), test_com_trial_o_texto_aparece(), test_plano_com_secretaria_mantem_o_trial(), test_plano_desconhecido_nao_tem_trial(), test_precheck_nao_tem_trial(), test_sem_trial_nao_ha_texto_de_espera_na_pagina()

### Community 85 - "env.py"
Cohesion: 0.29
Nodes (7): _do_run_migrations(), Connection, Alembic environment — async (asyncpg) configuration., Run migrations in 'offline' mode (emit SQL, no live DB connection)., Run migrations in 'online' mode using an async engine., run_migrations_offline(), run_migrations_online()

### Community 86 - "CHECKPOINT — Anexos no Portal (Brain-Message), parte 1: borda do brain-api"
Cohesion: 0.07
Nodes (29): 10.1 Sintoma, 10.2 Causa raiz — confirmada nos logs dos DOIS containers (não hipótese), 10.3 Correção, 10.4 O que o paciente vê agora, 10.5 Provas, 10.6 Achado fora do escopo (registrado, não corrigido), 10.7 Pendência do item 6 de `Z_QUESTÕES _PENDENTES.md` — GET do poll também segurava a conexão, 10. Incidente de produção 2026-09-18/19 — "upload com arquivo derruba o brain-api" (não derrubava) (+21 more)

### Community 87 - "0020_patient_accounts.py"
Cohesion: 0.43
Nodes (5): _backfill_accounts(), _backfill_invite_codes(), Connection, patient accounts: login só por e-mail + clínica entra na conta por convite  Re, upgrade()

### Community 88 - "0013_secretary_role.py"
Cohesion: 0.33
Nodes (5): downgrade(), secretary role: add `secretary` to the tenant role taxonomy (NO-OP by design), Intentionally empty — see the module docstring., Intentionally empty.      Deliberately does NOT delete or rewrite `secretary`, upgrade()

### Community 89 - "SecretariaResetIn"
Cohesion: 0.50
Nodes (3): Request schema for the brain-api -> secretaria admin proxy (CONTRACTS.md §11)., Body for `POST /admin/secretaria/reset` — a DESTRUCTIVE data wipe on secretaria., SecretariaResetIn

### Community 90 - "billing.py"
Cohesion: 0.10
Nodes (44): NoReturn, _add_product_preflight(), add_product_to_subscription(), AddProductPreflight, AddProductRequest, AddProductResult, AppliedSubscription, _apply_signup_intent_checkout() (+36 more)

### Community 91 - "CHECKPOINT — portal do paciente: login só por e-mail + clínica entra por convite (2026-09-15)"
Cohesion: 0.11
Nodes (17): §10 — Pendências, §1 — Causa raiz (reconfirmada no código antes de programar), §2 — Modelo escolhido: tabela de conta (opção a), §3 — O que entrou, §4 — Contrato HTTP novo (para o frontend), §5 — Compatibilidade na janela entre os deploys, §6 — Decisões tomadas sem consulta, §7 — Riscos aceitos (ditos, não escondidos) (+9 more)

### Community 92 - "test_billing_return_to.py"
Cohesion: 0.11
Nodes (25): `None` or an allowlisted keyword; anything else is 422 `unknown_return_to`., The `origem=…[&produto=precheck]` query the brain-frontend reads at `/checkout/s, The Checkout `success_url`: the CONFIGURED base + `return_query_for(...)`., return_query_for(), success_url_for(), validate_return_to(), _checkout(), `return_to` on POST /billing/checkout: allowlist + server-built success_url. (+17 more)

### Community 93 - "test_doctor_anamneses_parity.py"
Cohesion: 0.20
Nodes (21): _FakeResponse, _forbid_httpx(), _install_fake_httpx(), _owner(), Exception, MonkeyPatch, brain-api -> PreCheck proxies added by TASK-011 (anamneses parity).  Unit test, Configured PreCheck + an httpx client that records every request it gets. (+13 more)

### Community 97 - "CHECKPOINT — sessão de conta ativa pula pendente/e-mail/código ao abrir clínica nova (2026-09-24)"
Cohesion: 0.13
Nodes (14): §1 — Causa raiz (reconfirmada no código antes de programar), §2 — Decisões tomadas sem consulta (registradas), §3 — O contrato exato (partes 2 e 3 consomem isto), §4 — Segurança, §5 — Deploy, §6 — Validação (comandos e resultado reais, 2026-09-24), §7 — Pendências e o que NÃO foi feito, §8 — O nome do paciente acompanha a conta (2026-09-24, mesma sessão da parte 2) (+6 more)

### Community 98 - "CHECKPOINT — adicionar produto à assinatura (TASK C / TASK-015)"
Cohesion: 0.17
Nodes (11): 10. Ações posteriores do dono, 1. Estado, 2. Modelo, 3. Contrato HTTP, 4. Decisões aprovadas, 5. Desvios e correções de coerência, 6. Prova Stripe e Postgres, 7. Ordem de implantação (+3 more)

### Community 99 - "config.py"
Cohesion: 0.07
Nodes (30): FastAPI, Seed the single platform admin user (idempotent).  Reads ADMIN_EMAIL / ADMIN_P, Entitlements endpoint (CONTRACTS.md §3.1, stripe-billing-entitlements skill)., precheck_sso_token(), AsyncSession, SSO endpoint (CONTRACTS.md §5, auth-jwt-multitenant skill).  `POST /sso/preche, Mint a PreCheck session for the authenticated tenant's linked user.      `requ, Application configuration loaded from environment variables / .env file. (+22 more)

### Community 100 - "CHECKPOINT — PreCheck billing (plans, quota, top-up, tier upgrade)"
Cohesion: 0.13
Nodes (14): 1. Catalog (`services/catalog.py`), 2. Migration `0010_precheck_billing` (`down_revision = 0009_test_window`), 3. `services/precheck_billing.py` (new), 4. Internal PreCheck API (`api/internal_precheck.py`, `schemas/internal_precheck.py`, new), 5. Billing routes (`api/billing.py` + `services/billing.py`), 6. New settings (`config.py`, `.env.example`), At deploy, CHECKPOINT — PreCheck billing (plans, quota, top-up, tier upgrade) (+6 more)

### Community 101 - "PendingIdentityIn"
Cohesion: 0.30
Nodes (11): patient_contact(), Return the booking contact for a handle belonging to the requested clinic., PendingIdentityIn, Common key for secretarIA's pending-identity service leg.      Both fields mus, _FakeSession, Patient booking contact for the internal Brain-Message service boundary., test_falls_back_when_linked_account_has_no_address(), test_other_tenant_cannot_read_linked_account_email() (+3 more)

### Community 116 - "CHECKPOINT — Test window (Task 2: reframe the free trial as a Meta/WABA acceptance window)"
Cohesion: 0.13
Nodes (14): 1. `tenants` gains two columns (migration `0009_test_window`), 2. Window start — three triggers, all idempotent-safe, 3. Harden-at-`conectado` — the core billing-semantics change, 4. Internal cron contract — `GET /internal/onboarding/tenants`, 5. New one-shot cron event: `test_window_email_sent`, 6. Doctor-facing endpoints (`api/onboarding.py`, router-level `require_doctor`), CHECKPOINT — Test window (Task 2: reframe the free trial as a Meta/WABA acceptance window), Deviations from the literal contract (+6 more)

### Community 119 - "CHECKPOINT — papel `secretary` (secretária humana da clínica)"
Cohesion: 0.14
Nodes (13): 1. O que é o papel, 2. Migração 0013 é um NO-OP deliberado, 3. Gates — o papel é uma AMPLIAÇÃO, então a fronteira é explícita, 4. Endpoints novos (`api/onboarding.py`), 5. Sweep de `ROLE_MANAGER` (lugares que tipam/listam papéis), 6. Testes, 7. brain-frontend, 8. secretarIA-frontend (+5 more)

### Community 120 - "patient_access.py"
Cohesion: 0.13
Nodes (30): _authenticate_account(), _authenticate_patient(), _authenticate_pending(), _bearer_value(), get_current_account(), get_current_patient(), get_current_patient_login(), get_current_pending() (+22 more)

### Community 127 - "test_portal_enter_thread.py"
Cohesion: 0.25
Nodes (15): _logged_in(), `POST /patient-access/threads/{product}/enter` — the Portal speaks first on ENTR, F5 on a visit whose greeting never landed: the empty thread gets its greeting no, Record every mesh hop to the open route; answer the rest of the mesh with `{}`., Unlike `/pending`, a reload IS forwarded: whether to speak is secretarIA's call, _spy(), test_a_logged_in_patient_entering_asks_secretaria_to_speak(), test_a_pending_visitor_entering_is_forwarded_too() (+7 more)

### Community 128 - "brain-api — API & Data Contracts (Phase 1)"
Cohesion: 0.09
Nodes (23): 0. Cross-service boundaries & decisions (read first), 11.1 brain-api → PreCheck read proxy (supersedes §0's "not called"), 11.2 brain-api → secretaria admin connection (service-to-service), 11.3 Cross-mesh super-admin provisioning (`scripts/provision_superadmin.ps1`), 11.4 Admin "Modo médico" impersonation (`POST /admin/impersonate/token`), 11. Platform-admin API (RBAC round) — role `admin`, 14.1 Upstream contract (fixed — the other two services implement the receiving side), 14.2 Audit trail (+15 more)

### Community 129 - "6. Database tables (brain database)"
Cohesion: 0.14
Nodes (14): 6.1 `tenants` (non-sensitive identity/config), 6.2 `users`, 6.2a `staff_password_reset_codes` (e-mailed reset code — §2.6, migration `0030`), 6.3 `entitlements` (one row per tenant), 6.3a `refresh_tokens` (revocable session leg — §2.1a), 6.3b `processed_stripe_events` (webhook idempotency — §13.3), 6.3c `privacy_requests` (LGPD audit trail — §14), 6.3d `usage_events` (internal usage-event ledger — metering leg, §12.2) (+6 more)

### Community 130 - "CHECKPOINT — Onboarding state machine, multi-professional & billing Phase 1"
Cohesion: 0.15
Nodes (12): Billing Phase 1 (confirmed in `services/billing.py`, `services/usage.py`, `services/catalog.py`), CHECKPOINT — Onboarding state machine, multi-professional & billing Phase 1, Deviations from `CONTRACT_onboarding_v1.md` found while verifying, JWT / auth changes (confirmed in `core/security.py`, `api/deps.py`, `api/auth.py`), Known gaps / pending (external wiring — nothing left to build in this repo's code), Migration `0007_onboarding_state_machine` — what it actually adds, Migrations added this round (chain), Modules — what landed where (+4 more)

### Community 131 - "test_billing_add_product.py"
Cohesion: 0.30
Nodes (16): _aware(), add_product_to_subscription end to end against a fake Stripe (TASK C, spec §5.5, The webhook `updated` and the endpoint derive from the SAME subscription object, _req(), _state(), _tenant(), test_a_confirm_without_the_idempotency_key_is_refused_before_stripe(), test_a_response_without_the_requested_family_is_not_reported_as_added() (+8 more)

### Community 132 - "MessagePatientSession"
Cohesion: 0.06
Nodes (78): generate_refresh_token(), A high-entropy opaque token (the client-side value; only its hash is stored)., MessagePatientAccount, MessagePatientSession, The revocable long leg of a patient session — the sibling of `refresh_tokens`., One patient account: the proven address, and nothing else.      Created by the, account_clinics(), account_display_name() (+70 more)

### Community 133 - "CHECKPOINT — conta única multi-clínica do paciente (Brain-Message) (2026-09-12)"
Cohesion: 0.17
Nodes (11): §10 — Deploy e pendências, §1 — Ponto de partida (a explicação pro revisor), §2 — O que mudou, §3 — Contrato HTTP (o que o frontend precisa saber), §4 — Decisões tomadas sem perguntar (e dois desvios do texto do prompt), §5 — Por que confirmação explícita, e não vínculo por e-mail, §6 — Revisão de segurança, §7 — Registro de risco aceito: e-mail OTP e NIST SP 800-63B-4 (+3 more)

### Community 134 - "CHECKPOINT — Launch waitlist (pre-launch buy gate, backend half)"
Cohesion: 0.17
Nodes (11): 1. `models/waitlist_lead.py` (new) — `waitlist_leads`, 2. `schemas/waitlist.py` (new), 3. `services/waitlist.py` (new) — `upsert_waitlist_lead`, 4. `api/public_waitlist.py` (new) — `POST /public/launch-waitlist`, 5. Wiring, CHECKPOINT — Launch waitlist (pre-launch buy gate, backend half), Migration, Pendências (+3 more)

### Community 135 - "CHECKPOINT — Canal de entrega (whatsapp / brain_message) no modelo de dados"
Cohesion: 0.18
Nodes (10): 1. Dois booleanos, não um enum de canal — e no `Tenant`, não no `PlanDef`, 2. `channels` como objeto aninhado, não dois campos soltos em `EntitlementOut`, 3. O backfill é `connected_at IS NOT NULL` — e o prompt apontava para uma coluna que não existe, 4. `InternalEntitlementOut` ficou de fora, CHECKPOINT — Canal de entrega (whatsapp / brain_message) no modelo de dados, Decisões tomadas nesta sessão (e por quê), O que entrou onde, Pendências (+2 more)

### Community 136 - "CHECKPOINT — Meu Perfil (Part 1: self-edit foundation, `PATCH /doctor/me`)"
Cohesion: 0.20
Nodes (9): 1. `schemas/doctor.py` — `DoctorMeUpdateIn`, 2. `services/doctor.py` — `update_doctor_me`, 3. `api/doctor.py` — `PATCH /doctor/me`, CHECKPOINT — Meu Perfil (Part 1: self-edit foundation, `PATCH /doctor/me`), Decisions, Pendências, Tests, What changed, and where (+1 more)

### Community 137 - "CHECKPOINT — reset de senha nativo do brain-api"
Cohesion: 0.20
Nodes (9): brain-api (o grosso), brain-frontend, CHECKPOINT — reset de senha nativo do brain-api, Decisões que valem revisão, O bug que originou a rodada, O que entrou, por repo, Pendências, secretarIA-frontend (+1 more)

### Community 138 - "CHECKPOINT — as três faixas do PreCheck (50 / 100 / 300)"
Cohesion: 0.20
Nodes (9): 1. A tabela, 2. O que mudou no Stripe (conta live, feito em 2026-09-03), 3. O que mudou no código, 4. Ordem de deploy — não inverter, 5.1 Validação (local, 2026-09-03), 5.2 Prova em produção (2026-09-03, depois do deploy), 6. Pendências, CHECKPOINT — as três faixas do PreCheck (50 / 100 / 300) (+1 more)

### Community 139 - "Retenção de visitas vazias do Portal — TASK-042 (lado brain-api)"
Cohesion: 0.20
Nodes (9): A barreira de login, Deploy (não autorizado), E-mail digitado e nunca verificado — decisão do dono e risco registrado, Estado, O que entrou, Ordem anti-perda, Regra de "visita vazia", Retenção de visitas vazias do Portal — TASK-042 (lado brain-api) (+1 more)

### Community 140 - "CHECKPOINT — cupom de cortesia (acesso sem Stripe)"
Cohesion: 0.22
Nodes (8): 1. Por que não é um cupom de 100% off no Stripe, 2. O caminho, e onde ele se encaixa no pago, 3. Os portões (isto dá acesso pago de graça), 4. Estado em produção (medido em 2026-09-02), 5. O que entrou em 2026-09-02 (o fechamento) — NO AR, 6. Frase-gatilho: por que ela mudou junto, 7. Pendências, CHECKPOINT — cupom de cortesia (acesso sem Stripe)

### Community 141 - "CHECKPOINT — renovação silenciosa da sessão do paciente (Brain-Message) (2026-09-14)"
Cohesion: 0.22
Nodes (8): §1 — Causa raiz (reconfirmada no código atual, antes de programar), §2 — O que entrou, §3 — Os três riscos, e qual escolha resolveu cada um, §4 — Contrato HTTP para o frontend (`POST /patient-access/refresh`), §5 — Decisões tomadas sem consulta, §6 — Provas, §7 — Pendências, CHECKPOINT — renovação silenciosa da sessão do paciente (Brain-Message) (2026-09-14)

### Community 142 - "CHECKPOINT — contexto do agendamento no hand-off (nome + serviço)"
Cohesion: 0.22
Nodes (8): 1.1 Duas decisões de implementação que valem registro, 1. O que entrou neste repo, 2. Gate de entitlement — o que NÃO mudou, 3. PII, 4. Verificação, 5. Ordem de deploy — condição que bloqueia ESTA perna, 6. Pendências, CHECKPOINT — contexto do agendamento no hand-off (nome + serviço)

### Community 143 - "CHECKPOINT — Register at the first card (cold-signup login/entitlement split)"
Cohesion: 0.22
Nodes (8): CHECKPOINT — Register at the first card (cold-signup login/entitlement split), Deliberately NOT changed / open considerations, Files changed (backend, `brain-api`), Pending (to ship), Tests, The split (the architectural change), Update 2026-07-22 — corrections round: intent PATCH + checkout-config add-ons, Why

### Community 144 - "Cross-database LGPD erasure & portability — design"
Cohesion: 0.22
Nodes (8): Cross-database LGPD erasure & portability — design, Idempotency & partial-result semantics, Orchestration flow, Roles, The audit trail (`privacy_requests`), The fixed upstream contract, The "identifiers only as sha256" rule, The problem

### Community 145 - "2. Authentication"
Cohesion: 0.25
Nodes (8): 2.1 `POST /auth/token` — login (public), 2.1a `POST /auth/refresh` — rotate the session (public, rate-limited), 2.1b `POST /auth/logout` — revoke a refresh token (public), 2.1c The `__Host-refresh_token` cookie + `X-Brain-Client` (added 2026-08-31), 2.2 `GET /auth/me` — current identity (protected), 2.6.1 Anti-enumeration, 2.6 `POST /auth/password-reset/*` — forgotten-password recovery (public, rate-limited), 2. Authentication

### Community 146 - "CHECKPOINT — WhatsApp Coexistence onboarding (brain-api slice)"
Cohesion: 0.25
Nodes (7): CHECKPOINT — WhatsApp Coexistence onboarding (brain-api slice), Deviations from the prompt, External dependencies (nothing left to build in this repo's code), Pendências, Status, Tests added (`tests/test_onboarding_endpoints.py`), What landed this round

### Community 147 - "CHECKPOINT — Refresh token em cookie httpOnly `__Host-` + guarda CSRF"
Cohesion: 0.25
Nodes (7): Armadilha: "Modo médico" trocaria de identidade em silêncio, CHECKPOINT — Refresh token em cookie httpOnly `__Host-` + guarda CSRF, Detalhe de FastAPI que custa uma sessão, Gates, O que entrou, Pendências, Três decisões que parecem detalhe e não são

### Community 148 - "message_switchboard.py"
Cohesion: 0.18
Nodes (15): _capped(), _declared_length(), _media_failure(), _media_not_found(), MediaStream, open_media(), Response, The switchboard — one patient session, two product backends, zero text classific (+7 more)

### Community 149 - "13. Billing — Stripe (billing/auth round; `stripe-billing-entitlements`)"
Cohesion: 0.29
Nodes (7): 13.1 Webhook → entitlement recompute (the sole billing writer), 13.2 Product gates ripple automatically, 13.3 Fully-metered plan pricing + subscription trial (billing meter pricing round), 13.4 Charge hardening — ending a trial early on activation, 13.5 Catalog limit keys (metering only, no quota), 13.6 Trial-expiry cancellation (`trial_will_end` → `cancel_at`), 13. Billing — Stripe (billing/auth round; `stripe-billing-entitlements`)

### Community 150 - "16. Onboarding state machine & multi-professional (onboarding round)"
Cohesion: 0.29
Nodes (7): 16.1 State machine, 16.2 Doctor onboarding endpoints (`api/onboarding.py`, prefix `/doctor`), 16.3 Team: professionals, secretaries & invites (`api/onboarding.py`, prefix `/doctor`), 16.4 JWT `professional_id` claim, 16.5 Internal onboarding surface — secretaria's crons pull/push this, 16.6 Internal professional-email surface — who secretarIA may notify, 16. Onboarding state machine & multi-professional (onboarding round)

### Community 151 - "Hub token — quem vê a agenda inteira (`agenda_scope`) Implementation Plan"
Cohesion: 0.20
Nodes (9): File Structure, Global Constraints, Hub token — quem vê a agenda inteira (`agenda_scope`) Implementation Plan, Produces for secretarIA (exact wire shape), Review Focus, Self-Review, Task 1: The rule and the live resolver, Task 2: The introspection answers `agenda_scope` (+1 more)

### Community 152 - "10. Cross-product SSO — PreCheck handoff (implemented)"
Cohesion: 0.33
Nodes (6): 10.1 `POST /sso/precheck/token` — mint a PreCheck session (protected), 10.2 The minted token (how it conforms to PreCheck), 10.3 Lifetime, 10.4 Frontend handoff (same-origin), 10.5 Deployment invariant (REQUIRED) & onboarding, 10. Cross-product SSO — PreCheck handoff (implemented)

### Community 153 - "CHECKPOINT — toque real no Portal: o campo atravessa o switchboard (2026-09-12)"
Cohesion: 0.33
Nodes (5): §1 — O que mudou, §2 — Testes (em `tests/test_patient_access.py`), §3 — Deploy (NÃO FEITO), §4 — Prova ao vivo LOCAL (2026-09-12), CHECKPOINT — toque real no Portal: o campo atravessa o switchboard (2026-09-12)

### Community 154 - "Mesh secret rotation — zero-downtime procedures"
Cohesion: 0.33
Nodes (5): 1. `SECRET_KEY` (brain-api ↔ PreCheck — JWTs + SSO handoff), 2. The brain ↔ secretaria pair key (`SECRETARIA_API_KEY` = `INTERNAL_API_KEY`), 3. `SECRETARIA_ADMIN_TOKEN` (brain-api → secretaria `/admin/*`, X-Admin-Token), Mesh secret rotation — zero-downtime procedures, Rules that apply to every rotation

### Community 155 - "_call"
Cohesion: 0.20
Nodes (16): _attachment_ref(), _call(), list_messages(), mark_read(), _project_attachments(), Any, datetime, UUID (+8 more)

### Community 156 - "TASK-055 — URLs públicas por serviço"
Cohesion: 0.08
Nodes (20): brain-api, Documentação, graphify, Plano preparado — TASK-036, Prompts pendentes, Convite médico: recusa de plano mascarada como 502, Correção, Defeito reproduzido no código (+12 more)

### Community 157 - "test_test_window_add_product.py"
Cohesion: 0.28
Nodes (14): _ent(), _fakes(), _plans(), restart_test_window after TASK C (spec §5.8): never freezes a paying subscriptio, _settings(), _setup(), test_a_dual_clinic_pays_for_both_and_addons_ride_on_the_first(), test_a_manual_family_is_not_re_created_as_a_paid_item() (+6 more)

### Community 158 - "12. Doctor (tenant) API (RBAC round) — roles `doctor` / `manager` / `secretary`"
Cohesion: 0.33
Nodes (6): 12.1 brain-api → secretaria internal data connection (service-to-service), 12.2 secretarIA doctor-hub auth — hub token + introspection (billing/auth round), 12.3.1 Deploy order for the booking-context fields (FEAT 37–40), 12.3.2 `external_id` (a Portal patient) — the Portal leg, 12.3 secretarIA → PreCheck patient handoff (`POST /internal/precheck-handoff`), 12. Doctor (tenant) API (RBAC round) — roles `doctor` / `manager` / `secretary`

### Community 159 - "privacy.py"
Cohesion: 0.17
Nodes (24): _call_precheck(), _call_secretaria(), _erase_brain_patient(), erase_subject(), _export_brain_patient(), export_subject(), _leg_ok(), _normalize_subject_key() (+16 more)

### Community 160 - "CHECKPOINT — Hub token: quem vê a agenda inteira (`agenda_scope`) (TASK-044)"
Cohesion: 0.22
Nodes (8): CHECKPOINT — Hub token: quem vê a agenda inteira (`agenda_scope`) (TASK-044), Decisão de execução, Deploy (quando o dono autorizar), Estado, O que mudou, Pendências (fora deste plano), Regra, Validação

### Community 161 - "CHECKPOINT — abertura do Portal: e-mail já cadastrado é detectado e volta mascarado (2026-09-21)"
Cohesion: 0.20
Nodes (9): §1 — Causa raiz (reconfirmada no código antes de programar), §2 — Decisões desta rodada (fechadas, não reabrir sem o dono), §3 — O contrato exato que a parte 2 (secretarIA) vai consumir, §4 — Tensão registrada, não decidida em silêncio (`cross-tenant-account-linking`), §5 — PII: o que pode e o que não pode sair, §6 — Validação (comandos e resultado reais), §7 — O que a parte 2 (secretarIA) precisa fazer com isto, §8 — Pendências e o que NÃO foi feito (+1 more)

### Community 162 - "15. Cold signup — self-serve pay-and-provision (public, no human in the loop)"
Cohesion: 0.50
Nodes (4): 15.1 Endpoints, 15.1a Pre-checkout intake (`IntakeIn`), 15.2 Activation (webhook-driven; the sole entitlement-activation writer), 15. Cold signup — self-serve pay-and-provision (public, no human in the loop)

### Community 164 - "team.py"
Cohesion: 0.17
Nodes (17): invite(), list_team(), management(), AsyncSession, UUID, Managers administer only the clinic resolved from their live session., resend(), restore() (+9 more)

### Community 165 - "Request"
Cohesion: 0.10
Nodes (30): FormData, Receive, RequestValidationError, _attachment_parts(), _body_errors(), _CappedReceive, _checked_relay(), _declared_length() (+22 more)

### Community 166 - "8. Errors and status codes"
Cohesion: 0.18
Nodes (11): 8.1 Patient ↔ brain-api (`/patient-access/threads/*`), 8.2 brain-api ↔ secretarIA (`/internal/brain-message/*`), 8.3 brain-api ↔ PreCheck (`/internal/brain-message/*`), 8.4 Cross-cutting rule, 8.5 Message delivery state (enviado / entregue / lido / falhou) — secretarIA + brain-api, 8.6.1 Speak first on ENTRY — `POST /patient-access/threads/{product}/enter` (TASK-035, 2026-10-05), 8.6 The automation speaks first — `POST {SECRETARIA}/internal/brain-message/open`, 8.7 `email_masked` on `POST /internal/brain-message/pending-otp/request` (+3 more)

### Community 168 - "CHECKPOINT — Estado da mensagem (enviado / entregue / lido / falhou), parte 2: brain-api"
Cohesion: 0.22
Nodes (8): 1. O que o prompt supunha e o que o código mostrou, 2. O que mudou, 3. Contrato para o frontend (parte 3), 4. Decisões tomadas sem consulta, 5. Validação, 6. Deploy, 7. Pendências, CHECKPOINT — Estado da mensagem (enviado / entregue / lido / falhou), parte 2: brain-api

### Community 169 - "conftest.py"
Cohesion: 0.14
Nodes (10): db_session(), legacy_staff_invites(), mail_spy(), Test configuration.  pytest imports conftest before any test module, so config, A bare `AsyncSession` over a FRESH in-memory SQLite DB (all tables created)., Replace the real SMTP sender for every test (see tests/mail_spy.py)., Run a test against the legacy invite flow (staff credentials flag off)., staff_accounts_enabled() (+2 more)

### Community 170 - "CHECKPOINT — a automação fala primeiro no Portal (perna brain-api da TASK-003)"
Cohesion: 0.25
Nodes (7): 1. O que entrou, 2. Decisões tomadas sem perguntar, 3. Ordem de deploy — LIVRE nos dois sentidos, 4. Revisão, 5. Uma regressão causada e corrigida, 6. Pendências, CHECKPOINT — a automação fala primeiro no Portal (perna brain-api da TASK-003)

### Community 171 - "precheck_handoff"
Cohesion: 0.15
Nodes (14): precheck_handoff(), secretarIA -> brain-api -> PreCheck (CONTRACTS.md §12.3): pre-seed a WhatsApp, PrecheckHandoffIn, PrecheckHandoffOut, `POST /internal/precheck-handoff` body — secretarIA names the patient by tenant, One handle, never two, never none — `422` otherwise (TASK-003 §5.1)., `POST /internal/precheck-handoff` response — PreCheck's own 200 body passed, Any (+6 more)

### Community 172 - "Base"
Cohesion: 0.08
Nodes (29): DeclarativeBase, Base, get_session(), AsyncSession, Async SQLAlchemy 2.0 engine, session factory and declarative Base., Declarative base shared by every ORM model., FastAPI dependency that yields a database session., MessagePatientAccountOtp (+21 more)

### Community 173 - "upsert_waitlist_lead"
Cohesion: 0.18
Nodes (11): WaitlistLead model — "avise-me quando lançar" capture for the pre-launch gate., A visitor who asked to be notified when the product goes on sale., WaitlistLead, check_rate_limit(), AsyncSession, Launch-waitlist service: in-process anti-spam + IDEMPOTENT persistence.  Same, Return True if `client_ip` is under the per-minute limit, else False.      All, Apply a repeat submission onto an existing row (never touches `created_at`). (+3 more)

### Community 174 - "IdempotencyCache"
Cohesion: 0.20
Nodes (6): _Entry, IdempotencyCache, Idempotent patient sends — a retried message is relayed ONCE (2026-09-25).  A, (scope, key) → the one outcome of that message, in flight or delivered., `(result, replayed)`: `send()` runs at most once per live key; errors are not ke, T

### Community 175 - "refresh_config_status"
Cohesion: 0.22
Nodes (9): InternalOnboardingEventIn, `POST /internal/onboarding/tenants/{tenant_id}/events` body — one cron     book, apply_onboarding_event(), _ConfigStatusCacheEntry, _mode_resolved_or_fallback_due(), Any, Throttled pull of secretaria's config-status + the ativo-transition check., Apply one cron-reported event (`POST /internal/onboarding/tenants/{id}/events`, (+1 more)

### Community 176 - "test_config.py"
Cohesion: 0.28
Nodes (12): _origins(), Unit tests for `Settings.cors_origins` (config.py) — the JSON-array/comma-separa, test_blank_and_whitespace_entries_dropped(), test_empty_string(), test_json_array(), test_json_array_single_entry(), test_legacy_comma_separated(), test_malformed_json_array_fails_closed_not_raising() (+4 more)

### Community 177 - "_upstream"
Cohesion: 0.17
Nodes (12): discard_empty_visit(), merge_visit(), open_conversation(), open_precheck_session(), `(base_url, headers)` for `product`, or 503 when its leg of the mesh is unset., Ask secretarIA to open this patient's conversation and greet them — FIRE AND FOR, Tell secretarIA to throw a merged visit's conversation away — FIRE AND FORGET., Ask secretarIA to delete an EMPTY visit's conversation — and wait for the answer (+4 more)

### Community 178 - "SignupIntent"
Cohesion: 0.20
Nodes (11): SignupIntent model — cold-signup lead through Stripe Checkout to entitlement act, One cold-signup attempt: lead data + catalog selection through to provisioning., SignupIntent, OnboardingStatusOut, `GET /public/onboarding-status` response.      `products`/`onboarding_token` a, get_onboarding_status(), Resolve a Checkout Session id back to its signup intent's provisioning status., O `ready` de um intent já ativado: produtos liberados + token de onboarding. (+3 more)

### Community 179 - "7. Attachments / media — built end to end (2026-09-18; all three parts uncommitted, not deployed together)"
Cohesion: 0.25
Nodes (8): 7.1 Patient → brain-api: send a file, 7.2 brain-api → secretarIA (what part 2 must accept), 7.2a brain-api → PreCheck (2026-09-22), 7.3 Receive: the reference in the poll, the bytes in the media route, 7.4 Refusals — 4xx, never 5xx (permanent: resending the same file never passes), 7.5 secretarIA side (part 2) — built 2026-09-18, uncommitted, not deployed, 7.6 Frontend (part 3) — built 2026-09-18, uncommitted, not deployed, 7. Attachments / media — built end to end (2026-09-18; all three parts uncommitted, not deployed together)

### Community 181 - "secretaria_internal.py"
Cohesion: 0.29
Nodes (11): _empty_page(), _get_list(), list_appointments(), list_patients(), Any, UUID, Internal service-to-service DATA client into secretaria (`X-Internal-Api-Key`)., Tenant appointments — `GET /internal/tenants/{tenant_id}/appointments`.      ` (+3 more)

### Community 182 - "apply_stripe_event"
Cohesion: 0.10
Nodes (18): PrecheckTopupCredit, PreCheck top-up credit ledger — avulso consultations purchased on top of a PreCh, One granted PreCheck avulso purchase (`checkout.session.completed`,     `metada, ProcessedStripeEvent, Stripe webhook idempotency ledger (stripe-billing-entitlements skill).  Stripe, A Stripe event id we already applied. PK = the event id (natural dedupe key)., _apply_precheck_topup_checkout(), apply_stripe_event() (+10 more)

### Community 183 - "_stripe_post"
Cohesion: 0.15
Nodes (14): create_precheck_topup_checkout_session(), harden_charge(), Response, Create a ONE-OFF `mode=payment` Checkout Session for `quantity` avulso PreCheck, Swap a tenant's PreCheck subscription between the PreCheck tiers (Start / Basic, Stripe's own `error.code` / `error.message` off a failed response, for the WARNI, Form-encoded POST to the Stripe API. 503 when unconfigured, 502 on failure., GET counterpart of `_stripe_post` (same 503/502 error mapping). Reads of Stripe' (+6 more)

### Community 184 - "PrivacyRequestIn"
Cohesion: 0.40
Nodes (3): PrivacyRequestIn, Pydantic v2 schemas for the LGPD privacy orchestration (CONTRACTS.md §14).  Bo, Identify the subject of an erasure/export and confirm the (possibly destructive)

### Community 188 - "test_catalog_compose.py"
Cohesion: 0.21
Nodes (10): _ent(), SimpleNamespace, _quota(), catalog.compose_entitlement_state + the per-family readers (TASK C, spec §5.1)., Zero regression for one-family clinics: flags/addons/limits are what     comput, test_combo_wins_over_a_precheck_tier(), test_dual_clinic_anchors_on_secretaria_and_keeps_the_precheck_tier(), test_precheck_plan_of_covers_every_row_shape() (+2 more)

### Community 189 - "CHECKPOINT: clínica de teste criada pelo admin, sem Stripe (`POST /admin/tenants`)"
Cohesion: 0.25
Nodes (7): CHECKPOINT: clínica de teste criada pelo admin, sem Stripe (`POST /admin/tenants`), Contrato HTTP, Decisões tomadas sem consultar o dono, Nota para a auditoria de pontes, O que o endpoint faz, Provas, Remover antes do lançamento real (pedido do dono, 2026-09-25)

### Community 190 - "usage_summary"
Cohesion: 0.24
Nodes (13): _as_utc(), _calendar_month_window(), PrecheckUsageSummary, AsyncSession, datetime, quota_window(), PreCheck billing: quota window resolution + usage/spend summary (precheck-billin, Normalize a DB datetime for comparison (SQLite returns naive; Postgres aware) — (+5 more)

### Community 191 - "CHECKPOINT — cobertura das pontes de provisionamento (secretarIA / PreCheck)"
Cohesion: 0.29
Nodes (6): A regra, CHECKPOINT — cobertura das pontes de provisionamento (secretarIA / PreCheck), Decisões tomadas sem consulta, O problema, Provas, Tabela de cobertura (conferida no código em 2026-09-24)

### Community 193 - "setup_response"
Cohesion: 0.23
Nodes (10): complete(), NoPendingFirstAccess, AsyncSession, Request, Response, Only this surface accepts the staff_password_setup scope., resume(), setup_response() (+2 more)

### Community 194 - "1. Authentication"
Cohesion: 0.33
Nodes (6): 1.1 Patient ↔ brain-api, 1.2 Staff ↔ product (unaffected by this channel, listed for completeness), 1.3 brain-api ↔ product — the two internal keys, 1.4 The pending-visit reverse call (aside), 1.5 Internal booking contact (MVP Portal, Task 7A), 1. Authentication

### Community 195 - "test_hub_agenda_scope.py"
Cohesion: 0.26
Nodes (16): _clinics(), _create_user(), _owner_a_id(), _promote_to_manager(), Who sees the whole clinic agenda in secretarIA's hub (owner decision 2026-10-09, The same in-memory DB the `client` fixture serves (its get_session override)., test_a_promotion_counts_on_the_next_introspection_of_the_same_token(), test_a_refused_token_is_still_a_200_with_own() (+8 more)

### Community 196 - "PrecheckAccountLink"
Cohesion: 0.17
Nodes (13): link(), _parse_args(), Namespace, Link a brain user to their PreCheck user id — the SSO account-link (onboarding)., _resolve_user(), ensure_demo_link(), main(), Seed a development tenant + owner user + entitlement.  Idempotent: re-running (+5 more)

### Community 197 - "resolve_agenda_scope"
Cohesion: 0.21
Nodes (12): agenda_scope_for(), AsyncSession, UUID, Who sees the whole clinic agenda in secretarIA's hub (owner decision 2026-10-09, The rule, pure. Roles are the exact stored strings (models/user.py)., The scope of the hub session whose token names `actor` (the `act` claim) and, resolve_agenda_scope(), _tenant() (+4 more)

### Community 198 - "DemoRequest"
Cohesion: 0.33
Nodes (6): DemoRequest, DemoRequest model — isolated lead capture for the "Agendar demo" form.  Does N, A captured demo/lead request from the public marketing forms., _demo_request_dict(), _export_brain_user(), Brain's own contribution to a user export: the user row (id/email/name/role/

### Community 199 - "DoctorMeOut"
Cohesion: 0.19
Nodes (13): Identity-only view of a tenant (non-sensitive config)., Identity-only view of a user. NEVER declares `password_hash` (or any secret)., TenantOut, UserOut, AnamnesisStatusIn, DoctorMeOut, DoctorMeUpdateIn, HubTokenOut (+5 more)

### Community 200 - "IntakeIn"
Cohesion: 0.33
Nodes (6): IntakeIn, Pre-checkout intake answers (CONTRACT_onboarding_v1.md §7).      Stored verbat, The result of registering a lead: the intent (drives checkout) + the owner user, Registration, register (no intake) -> attach_intake -> provision reads the attached intake and, test_attach_intake_then_provision_seeds_onboarding_state()

### Community 204 - "redeem"
Cohesion: 0.25
Nodes (8): normalize(), plano_valido(), AsyncSession, Resgate de cupom de cortesia — ativa a clínica sem passar pelo Stripe.  O cami, O plano existe no catálogo? Usado ao cadastrar um cupom., Forma canônica do código: sem espaços das pontas e em maiúsculas.      Quem di, Resgata `code` para `intent_id` e devolve o intent já ativado.      Levanta 40, redeem()

### Community 205 - "test_stripe_proof_guard.py"
Cohesion: 0.39
Nodes (8): _calls(), Tests for the money-moving proof guard (loaded by path: scripts/ is not a packag, test_aborts_that_step_when_target_is_not_the_designated_one(), test_an_aborted_step_does_not_stop_the_next_one(), test_runs_only_on_the_exact_designated_pair(), test_skips_when_a_designation_var_is_blank(), test_skips_when_designation_env_is_absent(), test_skips_when_only_one_designation_var_is_set()

### Community 206 - "decode_token"
Cohesion: 0.13
Nodes (20): create_access_token(), create_patient_token(), decode_patient_account_token(), decode_patient_pending_token(), decode_patient_token(), decode_token(), Any, JWT issuance/validation and password hashing (the identity-authority primitives) (+12 more)

### Community 209 - "test_migration_0026_entitlement_product_state.py"
Cohesion: 0.43
Nodes (5): _as_list(), _load(), Migration 0026 — entitlements.precheck_plan / manual_products (TASK C, spec §5.1, test_revision_chain_and_id_length(), test_upgrade_adds_columns_and_backfills_manual_products()

### Community 211 - "test_auth_email_after_response.py"
Cohesion: 0.31
Nodes (8): _client(), _OrderRecorder, Authentication e-mail on PUBLIC routes leaves AFTER the response (TASK-056)., ASGI wrapper noting when the response body is fully sent., _record_mail(), test_patient_login_code_is_mailed_after_the_response(), test_staff_reset_code_is_mailed_after_the_response(), test_visit_code_is_mailed_after_the_response()

### Community 212 - "_send_precheck_attachment"
Cohesion: 0.25
Nodes (11): BinaryIO, CheckedAttachment, An upload that passed `check_attachment`: its REAL kind, a safe name, its real s, _file_chunks(), _off_the_loop(), Relay one patient message WITH a file to `product`'s inbound, as `multipart/form, PreCheck's file wire (2026-09-22): the RAW bytes, addressed by query, no multipa, The spooled upload, from its start, in chunks — read in a worker thread by the c (+3 more)

### Community 213 - "resolve_entitlement"
Cohesion: 0.10
Nodes (25): get_entitlements(), AsyncSession, Return the resolved entitlement state for the authenticated tenant.      `requ, enter_thread(), The trigger the Portal was missing (owner, 2026-10-05): before it, only a NEW vi, PatientEnterIn, AsyncSession, Resolve the entitlement state for a tenant from the local DB.      Loads the ` (+17 more)

### Community 214 - "CHECKPOINT — e-mails de autenticação no brain-api + "esqueci a senha" por código (TASK-056)"
Cohesion: 0.20
Nodes (9): brain-api, brain-frontend, Brain-Message-Frontend, CHECKPOINT — e-mails de autenticação no brain-api + "esqueci a senha" por código (TASK-056), O que entrou, por repo, Pendências, Provas, Publicação — o que o dono faz, nesta ordem (cada passo com autorização) (+1 more)

### Community 215 - "join_launch_waitlist"
Cohesion: 0.13
Nodes (12): join_launch_waitlist(), AsyncSession, Request, Public launch-waitlist endpoint (CONTRACTS.md §4 + §5): the PRE-LAUNCH buy gate., Capture a waitlist lead: honeypot + rate-limit guard, then upsert + confirm., Pydantic v2 schemas for the launch-waitlist vertical (CONTRACTS.md §4 + §5)., Body for `POST /public/launch-waitlist`.      `plan_hint` is deliberately NOT, Trim `name` and reject blank-after-trim (422). (+4 more)

### Community 216 - "test_billing_stripe_fixtures.py"
Cohesion: 0.40
Nodes (3): Replay of REAL (sanitized) Stripe subscription objects through the derivation (, Runs even with no fixtures: the loader and the derivation are importable togethe, test_the_replay_harness_itself_is_wired()

### Community 217 - "TASK-058 — Restauro do vínculo clínico (fase 2)"
Cohesion: 0.18
Nodes (9): Contrato e estados, Evidências, Implementação, Pendências e publicação, TASK-058 — Restauro do vínculo clínico (fase 2), Consumidor, Gestão e reenvio, POST /doctor/team/{user_id}/restore (+1 more)

### Community 218 - "stripe_proof_guard.py"
Cohesion: 0.67
Nodes (3): ProofResult, Guard for Stripe proof steps that MOVE MONEY (owner decision, 2026-09-29).  Th, run_money_step()

### Community 220 - "AdminTenantCreateIn"
Cohesion: 0.18
Nodes (8): create_tenant(), Create a clinic for testing, with only the products the admin picked turned on,, AdminTenantCreateIn, AdminTenantCreateOut, _check_password_composition(), Create a TEST clinic outside Stripe: tenant + active entitlement + owner, one ca, Result of `POST /admin/tenants`: the new tenant id, its entitlement as written a, Minimum composition: length is enforced by the Field; require letter + digit so

### Community 221 - "record_usage"
Cohesion: 0.25
Nodes (7): `POST /internal/usage-events` body — one billable/meterable action, already done, UsageEventIn, _forward_meter_event(), AsyncSession, Apply one usage event: idempotent ledger insert + entitlement counter increment., Best-effort Stripe meter-event forward for a metered feature (`billable_patients, record_usage()

### Community 222 - "invite_codes.py"
Cohesion: 0.27
Nodes (10): generate_invite_code(), normalize_invite_code(), parse_invite(), UUID, The clinic's patient invite: a short code a person can type, and the parser that, A fresh code from `secrets` (uniformly over the alphabet)., The stored spelling of a typed code, or `None` when it cannot be one., A clinic UUID, a normalized code, or `None` — never an error.      Anything sh (+2 more)

### Community 223 - "Checkpoint — digitando backend (TASK-033)"
Cohesion: 0.50
Nodes (3): Checkpoint — digitando backend (TASK-033), Navegador e deploy, Validação local

### Community 224 - "Conta pessoal de equipe — contrato congelado TASK-057"
Cohesion: 0.11
Nodes (14): APIs para a fase 3, Conta pessoal de equipe — contrato congelado TASK-057, Convites e credenciais, Fronteiras e fase 2, Identidade, permissões e armazenamento, Limites e baseline, Migração, matriz e ordem de rollout, Nota 2026-10-18 — padrões ligados (+6 more)

### Community 225 - "team_auto_email_acceptance.py"
Cohesion: 0.16
Nodes (11): Proxy, Loopback-only API/SMTP/browser fixture; no production data or integrations., SMTP, bearer(), check(), main(), password_from_last_mail(), Real HTTP and loopback SMTP: restore reissues pending first-access e-mail. (+3 more)

### Community 227 - "test_password_reset.py"
Cohesion: 0.27
Nodes (7): _code(), Staff password reset by e-mailed code — the HTTP surface (TASK-056).  Ground t, test_confirm_revokes_existing_sessions(), test_end_to_end_code_flow(), test_logs_never_carry_email_or_code(), test_wrong_code_and_unknown_email_get_the_same_400(), _token()

### Community 228 - "EntitlementPatchIn"
Cohesion: 0.29
Nodes (4): EntitlementPatchIn, Partial update of a tenant's entitlement (admin manual activation, pre-Stripe)., Only assignable catalog plans may be written (legacy aliases normalize)., test_admin_patch_without_a_subscription_records_manual_products()

### Community 229 - "test_patient_visit_merge.py"
Cohesion: 0.29
Nodes (10): An identity as a past verified login leaves it.      For the cases a live logi, _seed_identity(), Completing a visit discards its chat only when the clinic already knows the acco, A twin identity exists at the clinic => the visit's chat is discarded upstream., test_a_secretaria_that_refuses_the_merge_never_breaks_the_login(), test_completing_a_first_time_visit_does_not_merge_anything(), test_completing_a_superseded_visit_tells_secretaria_to_merge(), _verified_visit() (+2 more)

### Community 230 - "require_internal_api_key"
Cohesion: 0.50
Nodes (4): _internal_key_scheme, Security, Gate `/internal/*` on the shared pair key. Fail CLOSED; never log the candidate., require_internal_api_key()

### Community 231 - "request_pending_otp"
Cohesion: 0.22
Nodes (10): MessageOut, Send the code that will turn this conversation into an account.      THE BODY, Begin the login of the ACCOUNT.      TWO limiters before any DB work: the per-, request_otp(), request_pending_otp(), client_ip(), Request, Best-effort client IP for a per-IP limiter key.      The service runs behind n (+2 more)

### Community 232 - "PrivacyRequest"
Cohesion: 0.50
Nodes (3): PrivacyRequest, PrivacyRequest model — the LGPD audit trail for cross-database erasure/export., One LGPD erasure/export invocation (`POST /admin/privacy/erasure|export`).

### Community 233 - "entitlement.py"
Cohesion: 0.22
Nodes (9): ChannelsOut, PatientInviteOut, ProductsOut, Pydantic v2 schemas for the entitlements vertical (CONTRACTS.md §3.1).  The re, Per-product access flags (CONTRACTS.md §3.1 `products`).      `precheck` <- en, Per-channel delivery flags (CONTRACTS.md §3.1 `channels`).      `whatsapp` <-, `GET /entitlements/patient-invite` — what a clinic hands its patients so they ca, The second line, on its own: no offered product means nothing to greet into. (+1 more)

### Community 234 - "test_staff_account_sessions.py"
Cohesion: 0.29
Nodes (3): New account contract and clinical gate isolation, using the actual ASGI API., test_account_login_requires_selection_even_one_membership(), test_legacy_invite_writer_uses_restricted_first_access_when_enabled()

### Community 235 - "test_message_channels.py"
Cohesion: 0.20
Nodes (9): Delivery-channel columns on `tenants` + their `EntitlementOut` projection.  Th, `GET /entitlements` carries `channels` for both the entitled tenant (A) and the, A tenant created through the ORM gets the unified Portal by default (0023);, A raw INSERT that never mentions the columns still lands `false`/`true` (0023)., `connected_at IS NOT NULL` is the whole predicate.      `services/onboarding.p, test_backfill_marks_only_tenants_with_a_connected_number(), test_get_entitlements_serializes_channels(), test_new_tenant_is_born_on_brain_message_off_whatsapp() (+1 more)

### Community 236 - "Limpeza administrativa de pacientes por clínica — TASK-029"
Cohesion: 0.20
Nodes (8): Contrato e falhas, Escopo e preservação, Limpeza administrativa de pacientes por clínica — TASK-029, Operação pelo Swagger, Pré-requisitos de publicação, Validação e limites, Retenção de visitas vazias (TASK-042), Visita fundida na conta (TASK-034)

### Community 237 - "claim_pending_email"
Cohesion: 0.33
Nodes (6): claim_pending_email(), secretarIA -> brain-api: the visitor typed an address into the chat.      The, PendingEmailClaimIn, PendingEmailClaimOut, `POST /internal/brain-message/pending-email` — secretarIA captured an address in, `POST /internal/brain-message/pending-email` response.      `claimed` is the o

### Community 238 - "demo.py"
Cohesion: 0.28
Nodes (8): ProductInterestEnum, ProfileEnum, Pydantic v2 schemas for the demo-request vertical (CONTRACTS.md §4 + §5).  Pub, Who the lead is (CONTRACTS.md §4 `profile`). secretarIA-variant radio maps here., Which product the lead wants (CONTRACTS.md §4 `product_interest`).      brain-, Which surface the lead came from (CONTRACTS.md §4 `source`). Defaults to brain., SourceEnum, StrEnum

### Community 239 - "signup.py"
Cohesion: 0.31
Nodes (8): _as_utc(), exchange_onboarding_token(), OnboardingExchange, datetime, Cold-signup service: register-at-first-card, Stripe Checkout, onboarding poll +, A successful onboarding-token redemption: the resolved owner user + the intent i, Redeem a onboarding token minted by `GET /public/onboarding-status`.      Retu, Normalize a DB datetime for comparison (SQLite returns naive; Postgres aware).

### Community 240 - "0031_staff_accounts.py"
Cohesion: 0.50
Nodes (3): create_tables(), Add personal staff accounts without removing the legacy global email constraint., upgrade()

### Community 241 - "0032_staff_membership_email.py"
Cohesion: 0.50
Nodes (3): adopt_legacy_rows(), Contract step: retire global user email uniqueness only after account-aware code, upgrade()

### Community 242 - "TASK-057 — conta pessoal de equipe e vínculos clínicos"
Cohesion: 0.40
Nodes (5): Implementado, Limites e pendências, Provas locais já executadas, Retomada e correções da revisão, TASK-057 — conta pessoal de equipe e vínculos clínicos

### Community 243 - "main"
Cohesion: 0.36
Nodes (6): Message, bearer(), check(), main(), Real local brain API transitions preserve the actual SecretarIA database rows., uid()

### Community 244 - "AttachmentRefused"
Cohesion: 0.29
Nodes (5): Exception, AttachmentRefused, Any, A refusal the caller can act on. Carries its CODE only — never a name, never a b, The HTTP error body: a stable code, the sentence, and the ceiling when it applie

### Community 245 - "available_products"
Cohesion: 0.33
Nodes (6): available_products(), default_product(), 403 unless `product` is one this tenant may reach right now.      The SAME 403, The products this tenant may talk to on the Brain-Message channel, in tab order., The thread a link that names NO product opens on — the first one offered, or Non, require_product()

### Community 246 - "create_admin"
Cohesion: 0.50
Nodes (4): create_admin(), _parse_args(), Namespace, Create (or promote) a platform-admin user — full access to the /admin/* surface.

### Community 247 - "mark_thread_read"
Cohesion: 0.40
Nodes (4): mark_thread_read(), Relay the patient's read mark, scoped by the SESSION like every thread route., PatientReadMarkIn, `POST /patient-access/threads/{product}/messages/read` — "I have seen up to here

### Community 248 - "health.py"
Cohesion: 0.50
Nodes (3): health(), Health check endpoint., Liveness probe. Intentionally does not touch Postgres.

### Community 249 - "request_pending_otp_internal"
Cohesion: 0.50
Nodes (4): Service leg used by the post-booking hook; takes no address by design.      An, request_pending_otp_internal(), PendingOtpRequestOut, The code was accepted for delivery; neither address nor credential leaves.

### Community 250 - "AdminTenantOut"
Cohesion: 0.50
Nodes (4): AdminTenantOut, A tenant row for the admin tenants table.      Assembled by the service (joins, list_tenants(), Return one page of tenants (newest first) + the total count.      Assembles ea

### Community 251 - "PrecheckSsoTokenResponse"
Cohesion: 0.50
Nodes (3): PrecheckSsoTokenResponse, SSO handoff schemas (CONTRACTS.md §5).  The response of POST /sso/precheck/tok, A minted PreCheck session token + how long it is valid (seconds).

### Community 253 - "test_window_email_due"
Cohesion: 0.67
Nodes (3): datetime, Task 2 (Meta/WABA acceptance test-window reframe): whether     `GET /internal/o, test_window_email_due()

## Knowledge Gaps
- **468 isolated node(s):** `brain-api`, `EmailTemplate`, `Plano preparado`, `Plano preparado — TASK-036`, `Documentação` (+463 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **13 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `get_settings()` connect `get_settings` to `CheckoutSelection`, `User`, `_bearer`, `test_admin_tenant_create.py`, `MessagePatientSession`, `billing.py`, `select`, `test_catalog_entitlements.py`, `MessagePendingSession`, `test_refresh_cookie.py`, `_bearer`, `message_switchboard.py`, `create_checkout_session_for_intent`, `MessagePatient`, `Entitlement`, `admin.py`, `auth.py`, `_call`, `test_precheck_billing.py`, `privacy.py`, `team.py`, `Request`, `TokenResponse`, `_families_from_subscription`, `install_fake_settings`, `precheck_client.py`, `create_precheck_usage_event`, `precheck_handoff`, `select`, `conftest.py`, `Base`, `refresh_config_status`, `_upstream`, `SignupIntent`, `_request`, `secretaria_internal.py`, `open_pending`, `HTTPException`, `_stripe_post`, `issue_password_reset_code`, `_configure_mesh`, `test_visit_retention.py`, `test_smoke.py`, `test_staff_first_access.py`, `test_auth_hardening.py`, `cookies.py`, `secretaria_client.py`, `visit_retention.py`, `test_mailer.py`, `invite_existing`, `cleanup_patients`, `_account_body`, `decode_token`, `Settings`, `Tenant`, `_request`, `_send_precheck_attachment`, `billing.py`, `record_usage`, `config.py`, `require_internal_api_key`, `request_pending_otp`, `test_staff_account_sessions.py`, `request_pending_otp_internal`?**
  _High betweenness centrality (0.125) - this node is a cross-community bridge._
- **Why does `Tenant` connect `Tenant` to `test_onboarding.py`, `User`, `test_billing_add_product.py`, `MessagePatientSession`, `test_admin_tenant_create.py`, `_bearer`, `test_launch_waitlist.py`, `onboarding_sync.py`, `select`, `test_billing_phase1.py`, `_bearer`, `test_test_window.py`, `create_checkout_session_for_intent`, `test_portal_auto_greeting.py`, `MessagePatient`, `Entitlement`, `_tenant_ids`, `make_clinic`, `internal.py`, `team.py`, `TokenResponse`, `select`, `admin.py`, `Base`, `test_patient_account_name.py`, `get_settings`, `refresh_config_status`, `test_patient_pending_session.py`, `onboarding.py`, `open_pending`, `HTTPException`, `_stripe_post`, `apply_stripe_event`, `issue_password_reset_code`, `_configure_mesh`, `_tenant_com_precheck`, `test_visit_retention.py`, `test_staff_first_access.py`, `PrecheckAccountLink`, `invite_existing`, `DemoRequest`, `resolve_agenda_scope`, `IntakeIn`, `cleanup_patients`, `test_billing_checkout_guard.py`, `_account_body`, `FakeStripe`, `resolve_entitlement`, `billing.py`, `config.py`, `EntitlementPatchIn`, `test_message_channels.py`, `main`, `AdminTenantOut`, `test_window_email_due`?**
  _High betweenness centrality (0.076) - this node is a cross-community bridge._
- **Why does `_token()` connect `_bearer` to `test_admin_tenant_create.py`, `test_precheck_handoff.py`, `test_privacy.py`, `test_signup.py`, `test_billing_phase1.py`, `test_catalog_entitlements.py`, `test_test_window.py`, `_tenant_ids`, `test_test_window_add_product.py`, `test_precheck_billing.py`, `Base`, `test_doctor_secretaria.py`, `test_auth_hardening.py`, `test_professional_emails.py`, `test_hub_agenda_scope.py`, `test_billing_checkout_guard.py`, `FakeStripe`, `test_billing_return_to.py`, `test_doctor_anamneses_parity.py`, `test_message_channels.py`?**
  _High betweenness centrality (0.038) - this node is a cross-community bridge._
- **Are the 117 inferred relationships involving `Tenant` (e.g. with `seed()` and `require_billable_tenant()`) actually correct?**
  _`Tenant` has 117 INFERRED edges - model-reasoned connections that need verification._
- **Are the 159 inferred relationships involving `get_settings()` (e.g. with `seed_admin()` and `impersonate_token()`) actually correct?**
  _`get_settings()` has 159 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Alembic environment — async (asyncpg) configuration.`, `Run migrations in 'offline' mode (emit SQL, no live DB connection).`, `Run migrations in 'online' mode using an async engine.` to the rest of the system?**
  _1855 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `CheckoutSelection` be split into smaller, more focused modules?**
  _Cohesion score 0.09788359788359788 - nodes in this community are weakly interconnected._