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

    # --- Service-to-service (BFF proxy into precheck) ---
    # Base URL of the PreCheck backend, e.g. http://precheck:8000 on the internal
    # network (empty in dev disables the proxy routes' upstream call). brain-api proxies
    # admin "inbound" and doctor "anamneses" to precheck, FORWARDING the caller's brain
    # JWT (precheck validates it itself via brain_auth) — no separate internal key here.
    PRECHECK_BASE_URL: str = ""
    # Timeout (seconds) for the precheck proxy httpx client.
    PRECHECK_TIMEOUT_SECONDS: float = 10.0
    # Base URL of the (internal-only) secretaria service. Reserved: the /doctor
    # appointments + patients routes are stubs until secretaria exposes them; when it
    # does, brain-api will call it here with X-Internal-Api-Key (INTERNAL_API_KEY).
    SECRETARIA_BASE_URL: str = ""

    # --- Internal service-to-service key (into secretaria) ---
    # Sent as X-Internal-Api-Key when brain-api calls secretaria (fail-closed on the
    # secretaria side if unset). Unused while appointments/patients remain stubs.
    INTERNAL_API_KEY: str = ""

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
