"""Contract step: retire global user email uniqueness only after account-aware code."""

import sqlalchemy as sa
from alembic import op

revision = "0032_staff_membership_email"
down_revision = "0031_staff_accounts"
branch_labels = None
depends_on = None


def adopt_legacy_rows():
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "LOCK TABLE users, staff_password_reset_codes, refresh_tokens IN ACCESS EXCLUSIVE MODE"
        )
    )
    if bind.execute(
        sa.text("SELECT 1 FROM users GROUP BY lower(trim(email)) HAVING count(*)>1 LIMIT 1")
    ).first():
        raise RuntimeError("normalized staff email collision; explicit resolution required")
    bind.execute(
        sa.text("""
        INSERT INTO staff_accounts
            (id, email, name, password_hash, credential_version, requires_password_change,
             temporary_password_expires_at, reset_token_hash, reset_token_expires_at, created_at)
        SELECT id, lower(trim(email)), name, password_hash, 0, requires_password_change,
               temporary_password_expires_at, reset_token_hash, reset_token_expires_at, created_at
        FROM users WHERE tenant_id IS NOT NULL AND role != 'admin' AND account_id IS NULL
    """)
    )
    bind.execute(
        sa.text(
            "UPDATE users SET account_id = id WHERE account_id IS NULL "
            "AND tenant_id IS NOT NULL AND role != 'admin' "
            "AND id IN (SELECT id FROM staff_accounts)"
        )
    )
    bind.execute(sa.text("UPDATE users SET email = lower(trim(email))"))
    bind.execute(
        sa.text("""
        INSERT INTO staff_account_reset_codes
            (account_id, code_hash, expires_at, attempts, window_started_at, issued_in_window)
        SELECT c.user_id, c.code_hash, c.expires_at, c.attempts,
               c.window_started_at, c.issued_in_window
        FROM staff_password_reset_codes c JOIN staff_accounts a ON a.id = c.user_id
        ON CONFLICT (account_id) DO NOTHING
    """)
    )
    bind.execute(
        sa.text(
            "DELETE FROM staff_password_reset_codes "
            "WHERE user_id IN (SELECT id FROM staff_accounts)"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE users SET reset_token_hash = NULL, reset_token_expires_at = NULL "
            "WHERE account_id IS NOT NULL"
        )
    )


def upgrade():
    adopt_legacy_rows()
    # Serialize reservation of an email across the staff account and admin populations.
    # A database guard also covers scripts and a previous writer during rollout.
    op.execute("""
        CREATE FUNCTION guard_staff_identity_email() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.email := lower(trim(NEW.email));
            PERFORM pg_advisory_xact_lock(hashtextextended(NEW.email, 57032));
            IF TG_TABLE_NAME = 'users' THEN
                IF NEW.account_id IS NOT NULL THEN
                    IF NEW.role = 'admin' OR NEW.tenant_id IS NULL OR NOT EXISTS (
                        SELECT 1 FROM staff_accounts a
                        WHERE a.id=NEW.account_id AND a.email=NEW.email
                    ) THEN
                        RAISE EXCEPTION USING ERRCODE='23505',
                            MESSAGE='invalid staff identity binding';
                    END IF;
                ELSIF EXISTS (SELECT 1 FROM staff_accounts a WHERE a.email=NEW.email) THEN
                    RAISE EXCEPTION USING ERRCODE='23505',
                        MESSAGE='identity email already reserved';
                END IF;
            ELSE
                IF EXISTS (SELECT 1 FROM users u WHERE u.email=NEW.email
                           AND u.id != NEW.id AND u.account_id IS DISTINCT FROM NEW.id) THEN
                    RAISE EXCEPTION USING ERRCODE='23505',
                        MESSAGE='identity email already reserved';
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""CREATE TRIGGER guard_users_identity_email
        BEFORE INSERT OR UPDATE OF email, account_id, role, tenant_id ON users
        FOR EACH ROW EXECUTE FUNCTION guard_staff_identity_email()""")
    op.execute("""CREATE TRIGGER guard_accounts_identity_email
        BEFORE INSERT OR UPDATE OF email ON staff_accounts
        FOR EACH ROW EXECUTE FUNCTION guard_staff_identity_email()""")
    op.drop_index("ix_users_email", "users")
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("uq_users_tenant_email", "users", ["tenant_id", "email"], unique=True)
    op.create_index(
        "uq_users_unlinked_email",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("account_id IS NULL"),
    )
    op.create_index("uq_users_account_tenant", "users", ["account_id", "tenant_id"], unique=True)


def downgrade():
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM users GROUP BY email HAVING count(*) > 1 LIMIT 1")
    ).first():
        raise RuntimeError(
            "unsafe downgrade with multiple staff memberships; forward rollback only"
        )
    op.execute("DROP TRIGGER guard_users_identity_email ON users")
    op.execute("DROP TRIGGER guard_accounts_identity_email ON staff_accounts")
    op.execute("DROP FUNCTION guard_staff_identity_email()")
    op.drop_index("uq_users_account_tenant", "users")
    op.drop_index("uq_users_unlinked_email", "users")
    op.drop_index("uq_users_tenant_email", "users")
    op.drop_index("ix_users_email", "users")
    op.create_index("ix_users_email", "users", ["email"], unique=True)
