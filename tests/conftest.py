"""Test configuration.

pytest imports conftest before any test module, so configuring the environment
here guarantees `get_settings()` (lru_cached at first import of brain_api) reads
test values — letting the test modules use ordinary top-level imports.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("APP_ENV", "dev")

# Mesh upstreams are UNSET in tests: the proxy / internal-data clients then degrade to an
# empty page with no network. Force-empty here (real env beats the .env file in
# pydantic-settings) so a populated local `.env` (real URLs/keys) cannot bleed in and make
# these hermetic tests attempt real connections. Configured-path tests monkeypatch settings.
for _mesh_var in (
    "PRECHECK_BASE_URL",
    "SECRETARIA_BASE_URL",
    "INTERNAL_API_KEY",
    "SECRETARIA_ADMIN_TOKEN",
):
    os.environ[_mesh_var] = ""

# Re-export the seeded in-memory app fixture so any test module can request `client` by
# name (pytest injection) without importing it — avoids the F811 "redefinition" lint that
# importing a fixture and shadowing it as a parameter would otherwise trigger.
from tests.test_rbac import client  # noqa: E402, F401
