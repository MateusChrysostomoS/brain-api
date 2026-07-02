"""Application configuration loaded from environment variables / .env file."""

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
    # Access-token lifetime in minutes (keep short; this token cannot be revoked
    # before it expires).
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    # Lifetime of the PreCheck-compatible SSO handoff token (POST /sso/precheck/token).
    # This token BECOMES the PreCheck session (the ported dashboard stores it as its
    # `precheck_token` and uses it for every PreCheck-backend call), so its lifetime is
    # the PreCheck session length — matched to PreCheck's own `access_token_expire_minutes`
    # default of 60. The handoff is same-origin (written straight to localStorage, never
    # placed in a URL/Referer/log), so the usual "short bootstrap" rationale does not apply;
    # it is still short and unrevocable, per the auth-jwt-multitenant skill.
    PRECHECK_TOKEN_EXPIRE_MINUTES: int = 60

    # --- CORS (the Next.js Brain portal) ---
    # Comma-separated list of allowed browser origins for the portal.
    CORS_ALLOW_ORIGINS: str = "http://localhost:3000"

    # --- Demo request anti-spam ---
    # Max POST /demo-requests per client IP per minute (basic, in-process).
    DEMO_RATE_LIMIT_PER_MIN: int = 5

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
    # tenant_owner/tenant_staff that carries a tenant_id (else the endpoint returns 404; an
    # admin must never become a "doctor with no tenant"). Defaults to the seeded dev clinic
    # (scripts/seed_dev.py); in production set it to a real sandbox clinic owner's email.
    # This is admin-impersonation — a deliberate exception to "admin SSO is not wired"
    # (CONTRACTS §11.3); every mint is logged at WARNING with the acting admin + target.
    IMPERSONATION_DEMO_EMAIL: str = "dra.demo@clinica.com.br"

    # --- Service-to-service (BFF proxy into precheck) ---
    # Base URL of the PreCheck backend, e.g. http://precheck:8000 on the internal
    # network (empty in dev disables the proxy routes' upstream call). brain-api proxies
    # admin "inbound" and doctor "anamneses" to precheck, FORWARDING the caller's brain
    # JWT (precheck validates it itself via brain_auth) — no separate internal key here.
    PRECHECK_BASE_URL: str = ""
    # Timeout (seconds) for the precheck proxy httpx client.
    PRECHECK_TIMEOUT_SECONDS: float = 10.0
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

    @property
    def cors_origins(self) -> list[str]:
        """Parse CORS_ALLOW_ORIGINS into a clean list of origins."""
        return [o.strip() for o in self.CORS_ALLOW_ORIGINS.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (read once per process)."""
    return Settings()
