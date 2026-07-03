"""Test configuration.

pytest imports conftest before any test module, so configuring the environment
here guarantees `get_settings()` (lru_cached at first import of brain_api) reads
test values — letting the test modules use ordinary top-level imports.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("APP_ENV", "dev")
# The whole suite logs in dozens of times from one fake IP inside a minute; disable the
# per-IP auth limiter by default (the rate-limit tests monkeypatch their own limit).
os.environ.setdefault("AUTH_RATE_LIMIT_PER_MIN", "0")

# Mesh upstreams are UNSET in tests: the proxy / internal-data clients then degrade to an
# empty page with no network. Force-empty here (real env beats the .env file in
# pydantic-settings) so a populated local `.env` (real URLs/keys) cannot bleed in and make
# these hermetic tests attempt real connections. Configured-path tests monkeypatch settings.
for _mesh_var in (
    "PRECHECK_BASE_URL",
    "PRECHECK_INTERNAL_TOKEN",
    "SECRETARIA_BASE_URL",
    "SECRETARIA_API_KEY",
    "SECRETARIA_ADMIN_TOKEN",
):
    os.environ[_mesh_var] = ""

# Stripe billing (stripe-billing-entitlements skill, tests/test_billing.py). The webhook
# secret is a fixed test value so signed-payload tests can compute a real HMAC. The price
# map covers every catalog id the billing tests exercise. STRIPE_SECRET_KEY deliberately
# stays UNSET so no code path can ever hit the real Stripe API — checkout/portal happy-path
# tests monkeypatch settings instead.
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
os.environ.setdefault(
    "STRIPE_PRICE_MAP",
    (
        '{"complete_clinic_combo": "price_combo", "secretaria_ferro": "price_ferro", '
        '"secretaria_bronze_1": "price_b1", "precheck": "price_precheck", '
        '"multi_professional": "price_multipro", "reactivation_pack": "price_react", '
        '"ehr": "price_ehr"}'
    ),
)

# Re-export the seeded in-memory app fixture so any test module can request `client` by
# name (pytest injection) without importing it — avoids the F811 "redefinition" lint that
# importing a fixture and shadowing it as a parameter would otherwise trigger.
from tests.test_rbac import client  # noqa: E402, F401
