"""Application configuration loaded from environment variables / .env file."""

import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed settings. Values come from the environment or `.env`.

    Real environment variables always take precedence over the `.env` file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    APP_ENV: str = "dev"
    LOG_LEVEL: str = "INFO"

    # --- Database (asyncpg driver) — brain's own database ---
    DATABASE_URL: str = "postgresql+asyncpg://brain:brain@localhost:5432/brain"

    # --- JWT (identity authority) ---
    # HS256 signing key, shared with precheck (shared mesh secret). Generate with
    # `openssl rand -hex 64`. Empty in dev fails closed at token issue/verify time.
    SECRET_KEY: str = ""
    # Previous signing key, accepted for VERIFICATION ONLY during a rotation window
    # (tokens are always minted with SECRET_KEY). Set to the old key while rotating,
    # then clear once every token minted with it has expired. See docs/key-rotation.md.
    SECRET_KEY_PREVIOUS: str = ""
    # Access-token lifetime in minutes (keep short; this token cannot be revoked
    # before it expires — the refresh token below is the revocable long-lived leg).
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    # Refresh-token lifetime in days. Stored HASHED server-side (revocable) and
    # rotated on every use; a presented-again (already-rotated) token revokes the
    # user's whole refresh family (reuse = theft signal).
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14
    # Whether the HttpOnly refresh COOKIE (core/cookies.py) carries a Max-Age.
    # True (default): a persistent cookie that lives as long as the refresh token
    # itself, so a reload or a new tab resumes the session silently. False: a
    # SESSION cookie with no Max-Age, which the browser drops when it closes —
    # closer to the per-tab sessionStorage behaviour this replaced, and the right
    # setting for a clinic whose reception desk is a shared machine. Either way
    # the server-side token keeps its own REFRESH_TOKEN_EXPIRE_DAYS lifetime and
    # stays revocable; this only decides how long the BROWSER holds it.
    REFRESH_COOKIE_PERSISTENT: bool = True
    # Max POST /auth/token + /auth/refresh attempts per client IP per minute
    # (in-process sliding window, same machinery as the demo limiter; fail-open).
    # 0 disables (hermetic tests set 0; production keeps the default).
    AUTH_RATE_LIMIT_PER_MIN: int = 10
    # Lifetime of the tenant-scoped secretarIA HUB token minted by
    # POST /doctor/secretaria/hub-token (scope="secretaria_hub"; NOT a user JWT).
    # secretarIA introspects it against /internal/secretaria/hub-token/verify.
    HUB_TOKEN_EXPIRE_MINUTES: int = 60
    # Lifetime of the PreCheck-compatible SSO handoff token (POST /sso/precheck/token).
    # This token BECOMES the PreCheck session (the ported dashboard stores it as its
    # `precheck_token` and uses it for every PreCheck-backend call), so its lifetime is
    # the PreCheck session length — matched to PreCheck's own `access_token_expire_minutes`
    # default of 60. The handoff is same-origin (written straight to localStorage, never
    # placed in a URL/Referer/log), so the usual "short bootstrap" rationale does not apply;
    # it is still short and unrevocable, per the auth-jwt-multitenant skill.
    PRECHECK_TOKEN_EXPIRE_MINUTES: int = 60

    # --- CORS (the Next.js Brain portal + secretarIA-frontend) ---
    # Allowed browser origins, one per product frontend. Accepts a JSON array
    # (`["https://a.com","https://b.com"]`) or the legacy comma-separated form
    # (`https://a.com,https://b.com`) — see `cors_origins` below for parsing.
    CORS_ALLOW_ORIGINS: str = "http://localhost:3000"

    # --- Demo request anti-spam ---
    # Max POST /demo-requests per client IP per minute (basic, in-process).
    DEMO_RATE_LIMIT_PER_MIN: int = 5

    # --- Launch-waitlist anti-spam (pre-launch buy gate, services/waitlist.py) ---
    # Max POST /public/launch-waitlist per client IP per minute. Its OWN bucket, not the
    # /public/* signup one: a pre-launch pricing-page form must never be able to eat the
    # budget the real checkout funnel depends on once PRODUCT_LAUNCHED flips to true.
    WAITLIST_RATE_LIMIT_PER_MIN: int = 5

    # --- Cold signup anti-spam (public checkout -> auto-provisioning, services/signup.py) ---
    # Max requests per client IP per minute across the three /public/* signup routes
    # (signup-intents, checkout-sessions, onboarding-status share ONE bucket — same
    # in-process/fail-open SlidingWindowLimiter as demo/auth; no Redis).
    SIGNUP_RATE_LIMIT_PER_MIN: int = 10
    # Lifetime of the ONE-TIME onboarding token minted by GET /public/onboarding-status
    # once a signup intent finishes provisioning. Short-lived: the browser is expected to
    # redeem it via POST /auth/exchange-onboarding-token right away. Only its hash is
    # stored (signup_intents.onboarding_token_hash); every poll before redemption mints a
    # fresh one (rotates the hash), so an intercepted-but-unused token from an earlier
    # poll stops working immediately.
    ONBOARDING_TOKEN_EXPIRE_MINUTES: int = 15

    # --- Platform admin bootstrap (scripts/seed_admin.py) ---
    # Credentials for the single platform admin seeded into a fresh DB. The seed is
    # idempotent and reads ONLY from the environment — no admin password lives in code.
    # The password is bcrypt-hashed on insert and never logged.
    ADMIN_EMAIL: str = ""
    ADMIN_PASSWORD: str = ""

    # --- Admin "doctor mode" impersonation (the portal's "Modo médico" switch) ---
    # Email of the tenant (clinic) owner that an admin's one-click "Modo médico" enters.
    # POST /admin/impersonate/token mints a tenant-scoped doctor token for THIS user, so a
    # platform admin can use the doctor portal + PreCheck/secretarIA with REAL data while
    # developing the website + API — without a second login. The target MUST be a
    # doctor/manager that carries a tenant_id (else the endpoint returns 404; an
    # admin must never become a "doctor with no tenant"). Defaults to the seeded dev clinic
    # (scripts/seed_dev.py); in production set it to a real sandbox clinic owner's email.
    # This is admin-impersonation — a deliberate exception to "admin SSO is not wired"
    # (CONTRACTS §11.3); every mint is logged at WARNING with the acting admin + target.
    IMPERSONATION_DEMO_EMAIL: str = "dra.demo@clinica.com.br"

    # --- Service-to-service (BFF proxy into precheck) ---
    # Base URL of the PreCheck backend, e.g. http://precheck:8000 on the internal
    # network (empty in dev disables the proxy routes' upstream call). brain-api proxies
    # admin "anamneses"/"metrics" and doctor "anamneses" to precheck, FORWARDING the
    # caller's brain JWT (precheck validates it itself via brain_auth) — no separate
    # internal key here.
    PRECHECK_BASE_URL: str = ""
    # Timeout (seconds) for the precheck proxy httpx client.
    PRECHECK_TIMEOUT_SECONDS: float = 10.0
    # PreCheck's internal/n8n token (X-Internal-Token), used ONLY for the LGPD privacy
    # orchestration (services/privacy.py -> PreCheck POST /api/v1/internal/privacy/*,
    # CONTRACTS.md §14). A DIFFERENT mechanism from the forwarded-brain-JWT proxy above:
    # this is a service credential, not the caller's token. Empty here => the precheck
    # leg of an erasure/export reports "skipped_unconfigured" (the rest of the
    # orchestration still runs). NEVER logged.
    PRECHECK_INTERNAL_TOKEN: str = ""
    # Base URL of the (internal-only) secretaria service, e.g. http://secretaria:8000 on
    # the internal network. Used by the /doctor appointments + patients data calls
    # (services/secretaria_internal.py, with X-Internal-Api-Key) AND the admin connection
    # (services/secretaria_client.py, with X-Admin-Token). Empty in dev → the data calls
    # return an empty page; the admin routes raise 503.
    SECRETARIA_BASE_URL: str = ""

    # --- Internal service-to-service key (into secretaria) ---
    # ACTIVE: sent as X-Internal-Api-Key when brain-api calls secretaria's /internal/*
    # (appointments + patients for the doctor portal; services/secretaria_internal.py).
    # MUST equal secretaria's OWN INTERNAL_API_KEY byte-for-byte or secretaria rejects the
    # call (401/403); brain-api then surfaces 502. Empty here → the data calls fail closed
    # to an empty page (no upstream call). A random machine secret — NEVER logged. This is
    # a DIFFERENT mechanism from SECRETARIA_ADMIN_TOKEN below (X-Admin-Token / /admin/*).
    SECRETARIA_API_KEY: str = ""
    # Previous shared key, accepted for VERIFICATION ONLY on brain-api's own inbound
    # /internal/* surface during a rotation window (outbound calls always send the
    # current SECRETARIA_API_KEY). See docs/key-rotation.md.
    SECRETARIA_API_KEY_PREVIOUS: str = ""

    # --- secretaria admin (cross-API admin connection into secretaria's /admin/*) ---
    # secretaria has NO user/role system; its only privileged surface is `/admin/*`, guarded
    # by the `X-Admin-Token` header checked against secretaria's OWN `ADMIN_TOKEN` env var
    # (secretaria api/admin.py: require_admin). That is a DIFFERENT mechanism from
    # SECRETARIA_API_KEY/X-Internal-Api-Key above, so it needs its own value here. A brain
    # PLATFORM ADMIN cannot "log in" to secretaria — instead brain-api calls secretaria's
    # admin routes on the admin's behalf, presenting this token. It MUST equal secretaria's
    # ADMIN_TOKEN byte-for-byte or secretaria returns 403; empty here fails closed (brain-api
    # raises 503 before any call). It guards a DESTRUCTIVE wipe endpoint — treat it as a
    # production credential and NEVER log it.
    SECRETARIA_ADMIN_TOKEN: str = ""
    # Timeout (seconds) for the secretaria admin httpx client.
    SECRETARIA_TIMEOUT_SECONDS: float = 10.0

    # --- Stripe billing (stripe-billing-entitlements skill) ---
    # Secret API key (sk_test_... / sk_live_...). Empty disables the billing endpoints
    # (503 billing_not_configured) — reads NEVER call Stripe either way.
    STRIPE_SECRET_KEY: str = ""
    # Webhook endpoint signing secret (whsec_...) for POST /webhooks/stripe. Empty
    # fails CLOSED: the webhook rejects every delivery (400).
    STRIPE_WEBHOOK_SECRET: str = ""
    # JSON object mapping catalog ids (plans AND add-ons, services/catalog.py) to
    # Stripe price ids, e.g. {"secretaria_basico": "price_123", "ehr": "price_456"}.
    # Per-environment (test vs live prices); the catalog itself never hardcodes one.
    STRIPE_PRICE_MAP: str = "{}"
    # Where Stripe Checkout / the Billing Portal send the browser back to.
    STRIPE_CHECKOUT_SUCCESS_URL: str = "http://localhost:3000/app?checkout=success"
    STRIPE_CHECKOUT_CANCEL_URL: str = "http://localhost:3000/app?checkout=cancelled"
    # Cold-signup checkouts land on a PUBLIC onboarding page (the buyer has no session
    # yet, so /app would bounce them to /login). Empty = fall back to the shared URLs
    # above. The success URL should carry Stripe's {CHECKOUT_SESSION_ID} template so the
    # page can poll GET /public/onboarding-status.
    STRIPE_SIGNUP_CHECKOUT_SUCCESS_URL: str = ""
    STRIPE_SIGNUP_CHECKOUT_CANCEL_URL: str = ""
    STRIPE_PORTAL_RETURN_URL: str = "http://localhost:3000/app"
    # Stripe API base + client timeout (base overridable only for local stubs).
    STRIPE_API_BASE: str = "https://api.stripe.com"
    STRIPE_TIMEOUT_SECONDS: float = 15.0
    # Currency for the cold-signup SETUP-mode Checkout Session. Required by Stripe there
    # and there only: a setup session carries no line items, so it cannot infer a currency
    # the way subscription mode does (which reads it off the prices) — omitting it makes
    # Stripe reject the request outright. Drives which payment methods the page offers.
    STRIPE_SETUP_CURRENCY: str = "brl"
    # Trial length (days) when > 0, applied in two shapes: as
    # subscription_data[trial_period_days] on every subscription-mode Checkout Session
    # (services/billing.py::_apply_trial, the authenticated upsell), and as a TOP-LEVEL
    # trial_period_days on the subscription the cold-signup webhook creates
    # (services/billing.py::_create_subscription_for_signup — that path's Checkout Session
    # is mode=setup and carries no subscription). Default 0/off; deploy sets it high
    # enough (e.g. ~75) to outlast the onboarding retry window before Stripe would
    # otherwise charge a still-unconnected tenant (services.billing.harden_charge ends
    # the trial early once the tenant reaches 'ativo', regardless of this value).
    STRIPE_TRIAL_PERIOD_DAYS: int = 0
    # Stripe Meter event_name for the billable_patients metered add-on price
    # (services/usage.py forwards a meter event after a successful billable_patients
    # usage record when this AND the tenant's stripe_customer_id are both set). Unset
    # disables the forward entirely (metering-only, no billing impact either way).
    STRIPE_METER_EVENT_BILLABLE_PATIENTS: str | None = None
    # Stripe Meter event_name for the active_professionals metered PLAN price (the fully
    # metered secretaria_basico model, R$80/active professional/month). Same forwarding
    # contract as STRIPE_METER_EVENT_BILLABLE_PATIENTS above: services/usage.py forwards a
    # meter event after a successful active_professionals usage record when this AND the
    # tenant's stripe_customer_id are both set; unset disables the forward entirely
    # (metering-only, no billing impact either way).
    STRIPE_METER_EVENT_ACTIVE_PROFESSIONALS: str | None = None
    # Stripe Meter event_name for the reminders metered PLAN price (24h/1h appointment
    # reminders sent OUTSIDE the WhatsApp 24h window — the third leg of the fully metered
    # secretaria_basico model, alongside billable_patients/active_professionals above).
    # Same forwarding contract: services/usage.py forwards a meter event after a
    # successful "reminders" usage record (emitted by secretarIA's reminders plugin, one
    # per billable HSM template send) when this AND the tenant's stripe_customer_id are
    # both set; unset disables the forward entirely (metering-only, no billing impact
    # either way).
    STRIPE_METER_EVENT_REMINDERS: str | None = None

    # --- PreCheck billing (precheck-billing round) ---
    # PreCheck bills FLAT-PRICE-plus-quota, never metered (unlike secretaria_basico above):
    # each PreCheck plan grants a monthly consultation quota (services/catalog.py's
    # LIMIT_PRECHECK_CONSULTATIONS), read from these two settings AT CATALOG IMPORT TIME
    # (services/catalog.py's PLANS construction) rather than hardcoded like every other
    # plan limit — a deliberate exception so an operator can retune the quota (a promo, a
    # temporary bump) with an env change + restart, no code deploy. Changing these after
    # the process has started has NO effect until restart (catalog.py reads them once).
    PRECHECK_BASIC_CONSULTATIONS_PER_MONTH: int = 100
    PRECHECK_ADVANCED_CONSULTATIONS_PER_MONTH: int = 300
    # Bounds on ONE avulso purchase of PreCheck consultations (POST /billing/precheck/topup,
    # a one-off mode=payment Checkout Session — services/billing.py::
    # create_precheck_topup_checkout_session). The top-up Price is per-UNIT (one
    # consultation), so the doctor names the quantity in the BRAIN UI and we bill
    # `quantity x unit price` — there is no fixed pack size. The minimum makes a purchase
    # worth processing; the maximum is only a typo/abuse guard, not a business rule.
    # Both are enforced server-side regardless of what the frontend sends, and the
    # RESULTING quantity is stamped into the Checkout Session's `metadata.quantity` AT
    # PURCHASE TIME — the webhook grant reads it back from the event's own metadata, never
    # from live settings, so retuning these can never alter a purchase already paid for.
    PRECHECK_TOPUP_MIN_QUANTITY: int = 5
    PRECHECK_TOPUP_MAX_QUANTITY: int = 1000
    # Shared secret PAIR for PreCheck's OWN inbound `/internal/precheck/*` surface
    # (api/internal_precheck.py: X-Internal-Api-Key, secrets.compare_digest, fail CLOSED
    # when unset). A SEPARATE service identity from SECRETARIA_API_KEY above — PreCheck
    # must never be able to call brain-api using secretarIA's key or vice versa (each mesh
    # caller gets its own credential, auth-jwt-multitenant skill). NEVER logged.
    PRECHECK_API_KEY: str = ""
    # Rotation window ONLY: the previous key, accepted for VERIFICATION ONLY while both
    # services flip to the new value (docs/key-rotation.md). Leave empty otherwise.
    PRECHECK_API_KEY_PREVIOUS: str = ""

    # --- Onboarding / multi-professional (CONTRACT_onboarding_v1.md) ---
    # Meta Graph API app credentials for the WhatsApp Embedded Signup authorization-code
    # exchange (POST /doctor/onboarding/attempts -> services/meta_graph.py). Empty ->
    # the exchange is skipped/fails soft: an attempt carrying a `code` with no configured
    # app credentials just records as a normal 'fail' attempt
    # (error_code="token_exchange_failed"), never a 500.
    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    META_GRAPH_BASE_URL: str = "https://graph.facebook.com/v23.0"
    # Meta Embedded Signup config id. The FRONTEND needs its own copy
    # (NEXT_PUBLIC_META_APP_ID / NEXT_PUBLIC_META_ES_CONFIG_ID) to drive the JS SDK; this
    # is the backend's copy, surfaced read-only via GET /doctor/onboarding's
    # `embedded_signup` block so the portal can tell whether the flow is configured
    # without hardcoding a second source of truth. Not a secret (Meta's JS SDK takes an
    # equivalent config id client-side too) — unlike META_APP_SECRET, safe to echo back.
    META_ES_CONFIG_ID: str = ""
    # The Embedded Signup `extras.featureType` value that turns on the Coexistence flow
    # (onboarding of an existing WhatsApp Business app user, as opposed to a brand-new
    # number) — echoed read-only via GET /doctor/onboarding's `embedded_signup` block
    # (schemas/onboarding.py::EmbeddedSignupOut.coexistence_feature_type) so the frontend
    # can decide whether to offer "já uso este número no WhatsApp Business" alongside the
    # standard new-number flow. Empty ⇒ the coexistence option is not offered in the
    # portal. Not a secret (Meta's JS SDK takes the same string client-side) — unlike
    # META_APP_SECRET, safe to echo back.
    META_ES_COEXISTENCE_FEATURE_TYPE: str = "whatsapp_business_app_onboarding"
    # Base URL of the brain-frontend portal — used to build the professional-invite link
    # (POST /doctor/professionals/invites -> "{FRONTEND_BASE_URL}/convite?token=...").
    FRONTEND_BASE_URL: str = "http://localhost:3000"
    # Fallback for the ativo transition when secretaria's Coexistence "mode resolved"
    # signal (history/smb_app_state_sync webhook fields, secretaria-side) never arrives:
    # a tenant whose WhatsApp `connected_at` is older than this many hours is treated as
    # mode-resolved anyway, so a stalled/missing signal can never permanently block
    # activation (services/onboarding_sync.py refresh_config_status).
    MODE_RESOLVE_FALLBACK_HOURS: int = 24
    # Throttle for the config-status pull from secretaria (services/onboarding_sync.py,
    # in-process TTL cache — no Redis in this service): GET /doctor/onboarding /
    # GET /doctor/professionals refresh at most once per this many seconds per tenant.
    CONFIG_STATUS_PULL_TTL_SECONDS: int = 60
    # Lifetime of a professional-invite token (POST /doctor/professionals/invites ->
    # POST /auth/exchange-invite-token). Same hashed-at-rest / single-use scheme as the
    # onboarding token (ONBOARDING_TOKEN_EXPIRE_MINUTES) but expressed in hours (a human
    # is expected to check their invite email within days, not minutes).
    INVITE_TOKEN_EXPIRE_HOURS: int = 72
    # Lifetime of a password-reset token (POST /auth/password-reset/request ->
    # /confirm). Expressed in MINUTES, not hours like the invite above: a reset link is
    # a credential-recovery secret sitting in an inbox, so it is deliberately the
    # shortest-lived token in the service. 30 minutes matches what PreCheck's own reset
    # flow has used since it shipped (app/services/password_reset.py), so a user who has
    # accounts in both products sees the same window.
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 30

    @property
    def cors_origins(self) -> list[str]:
        """Parse CORS_ALLOW_ORIGINS into a clean list of origins.

        Accepts a JSON array (`["https://a.com","https://b.com"]`) — the value
        starts with `[` — or falls back to the legacy comma-separated form
        (`https://a.com,https://b.com`); a malformed JSON array degrades to
        empty rather than raising, so a typo in EasyPanel fails closed (CORS
        rejects everyone) instead of crashing every request through this
        service (`services/entitlements.py` and every DB-touching route sit
        behind the same process, so `Settings()` must never raise here).

        Trailing slashes are stripped because Starlette's CORSMiddleware
        matches the request's `Origin` header EXACTLY, and an origin is
        scheme+host+port by definition — a browser never sends the trailing
        slash. Configuring `https://app.example.com/` would therefore reject
        every real browser request with a 400 "Disallowed CORS origin" while
        looking correct in the panel. Surrounding quotes are stripped for the
        same reason: some deploy panels persist the value with the quotes
        included. Mirrors secretarIA's `config.py::cors_origins` hardening
        (same bug class bit that service's CORS_ALLOW_ORIGINS on 2026-07-29).
        """
        raw = self.CORS_ALLOW_ORIGINS.strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = []
            entries = [str(o) for o in parsed] if isinstance(parsed, list) else []
        else:
            entries = raw.split(",")
        origins: list[str] = []
        for entry in entries:
            origin = entry.strip().strip("'\"").rstrip("/")
            if origin:
                origins.append(origin)
        return origins

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (read once per process)."""
    return Settings()
