"""Test configuration.

pytest imports conftest before any test module, so configuring the environment
here guarantees `get_settings()` (lru_cached at first import of brain_api) reads
test values — letting the test modules use ordinary top-level imports.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("APP_ENV", "dev")
