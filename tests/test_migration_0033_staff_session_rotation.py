"""Migration 0033 — staff_account_sessions.previous_token_hash / rotated_at (TASK-065).

Hermetic: loads the migration FILE and runs `upgrade()`/`downgrade()` against an OLD-shaped
`staff_account_sessions` table on in-memory SQLite (the suite's `create_all` proves nothing
about a migration).
"""

import importlib.util
import pathlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from brain_api.models import StaffAccountSession

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0033_staff_session_rotation.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("mig_0033", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain_and_id_length():
    module = _load()
    assert module.revision == "0033_staff_session_rotation"
    assert module.down_revision == "0032_staff_membership_email"
    # alembic_version.version_num is VARCHAR(32): a longer id fails on Postgres.
    assert len(module.revision) <= 32


def test_model_declares_both_nullable_columns():
    table = StaffAccountSession.__table__
    assert table.c.previous_token_hash.nullable is True
    assert table.c.previous_token_hash.type.length == 64
    assert table.c.rotated_at.nullable is True


def _columns(conn) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns("staff_account_sessions")}


def test_upgrade_is_additive_and_downgrade_restores_the_old_shape():
    module = _load()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE staff_account_sessions ("
                "id VARCHAR(36) PRIMARY KEY, account_id VARCHAR(36) NOT NULL, "
                "token_hash VARCHAR(64) NOT NULL UNIQUE, credential_version INTEGER NOT NULL, "
                "expires_at DATETIME NOT NULL, revoked_at DATETIME, created_at DATETIME)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO staff_account_sessions "
                "(id, account_id, token_hash, credential_version, expires_at) "
                "VALUES ('s1', 'a1', 'h1', 0, '2030-01-01')"
            )
        )
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        assert {"previous_token_hash", "rotated_at"} <= _columns(conn)
        indexes = {i["name"] for i in sa.inspect(conn).get_indexes("staff_account_sessions")}
        assert "ix_staff_account_sessions_previous_token_hash" in indexes
        row = conn.execute(
            sa.text(
                "SELECT token_hash, previous_token_hash, rotated_at FROM staff_account_sessions"
            )
        ).one()
        assert tuple(row) == ("h1", None, None)

        with Operations.context(MigrationContext.configure(conn)):
            module.downgrade()
        assert not ({"previous_token_hash", "rotated_at"} & _columns(conn))
        assert conn.execute(sa.text("SELECT token_hash FROM staff_account_sessions")).scalar() == (
            "h1"
        )
