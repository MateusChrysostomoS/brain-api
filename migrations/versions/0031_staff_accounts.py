"""Add personal staff accounts without removing the legacy global email constraint."""

import sqlalchemy as sa
from alembic import op

revision = "0031_staff_accounts"
down_revision = "0030_staff_password_reset_codes"
branch_labels = None
depends_on = None


def create_tables():
    # Frozen revision: never import application ORM models into a historical migration.
    op.create_table(
        "staff_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "requires_password_change", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("temporary_password_expires_at", sa.DateTime(timezone=True)),
        sa.Column("reset_token_hash", sa.String(64)),
        sa.Column("reset_token_expires_at", sa.DateTime(timezone=True)),
        sa.Column("preferred_user_id", sa.Uuid()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_staff_accounts_email", "staff_accounts", ["email"], unique=True)
    op.create_index("ix_staff_accounts_reset_token_hash", "staff_accounts", ["reset_token_hash"])
    op.create_table(
        "staff_account_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("staff_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_staff_account_sessions_account_id", "staff_account_sessions", ["account_id"]
    )
    op.create_index(
        "ix_staff_account_sessions_token_hash",
        "staff_account_sessions",
        ["token_hash"],
        unique=True,
    )
    op.create_table(
        "staff_account_reset_codes",
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("staff_accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_in_window", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "staff_membership_invites",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("membership_version", sa.Integer(), nullable=False),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("staff_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("is_manager", sa.Boolean(), nullable=False),
        sa.Column("professional_id", sa.Uuid()),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("delivery_status", sa.String(32), nullable=False),
        sa.UniqueConstraint("account_id", "tenant_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_staff_membership_invites_account_id", "staff_membership_invites", ["account_id"]
    )


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("staff account data migration requires PostgreSQL")
    # Refuse ambiguity instead of merging independently created identities.
    collisions = bind.execute(
        sa.text("SELECT 1 FROM users GROUP BY lower(trim(email)) HAVING count(*) > 1 LIMIT 1")
    ).first()
    if collisions:
        raise RuntimeError("normalized staff email collision; explicit resolution required")
    create_tables()
    op.add_column("users", sa.Column("account_id", sa.Uuid(), nullable=True))
    op.add_column("refresh_tokens", sa.Column("credential_version", sa.Integer(), nullable=True))
    bind.execute(
        sa.text("""UPDATE refresh_tokens r SET credential_version=u.credential_version
        FROM users u WHERE r.user_id=u.id""")
    )
    op.add_column(
        "users",
        sa.Column("membership_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_foreign_key(
        "fk_users_staff_account", "users", "staff_accounts", ["account_id"], ["id"]
    )
    op.create_index("ix_users_account_id", "users", ["account_id"])


def downgrade():
    bind = op.get_bind()
    unsafe = bind.execute(
        sa.text("""
        SELECT 1 FROM staff_accounts a LEFT JOIN users u ON u.account_id = a.id
        GROUP BY a.id HAVING count(u.id) != 1 LIMIT 1
    """)
    ).first()
    pending = bind.execute(sa.text("SELECT 1 FROM staff_membership_invites LIMIT 1")).first()
    if unsafe or pending:
        raise RuntimeError("unsafe staff account downgrade; retain account-aware code and schema")
    bind.execute(
        sa.text("""
        UPDATE users u SET password_hash=a.password_hash,
            requires_password_change=a.requires_password_change,
            temporary_password_expires_at=a.temporary_password_expires_at,
            reset_token_hash=a.reset_token_hash, reset_token_expires_at=a.reset_token_expires_at,
            access_revoked_at=coalesce(u.access_revoked_at,a.disabled_at),
            credential_version=u.credential_version + 1
        FROM staff_accounts a WHERE u.account_id=a.id
    """)
    )
    bind.execute(
        sa.text("""
        INSERT INTO staff_password_reset_codes
            (user_id, code_hash, expires_at, attempts, window_started_at, issued_in_window)
        SELECT u.id, c.code_hash,
               CASE WHEN c.consumed_at IS NULL THEN c.expires_at ELSE least(c.expires_at,now()) END,
               c.attempts, c.window_started_at, c.issued_in_window
        FROM staff_account_reset_codes c JOIN users u ON u.account_id=c.account_id
        ON CONFLICT (user_id) DO UPDATE SET code_hash=EXCLUDED.code_hash,
            expires_at=EXCLUDED.expires_at, attempts=EXCLUDED.attempts,
            window_started_at=EXCLUDED.window_started_at, issued_in_window=EXCLUDED.issued_in_window
    """)
    )
    bind.execute(sa.text("UPDATE refresh_tokens SET revoked_at=now() WHERE revoked_at IS NULL"))
    op.drop_index("ix_users_account_id", "users")
    op.drop_constraint("fk_users_staff_account", "users", type_="foreignkey")
    op.drop_column("users", "account_id")
    op.drop_column("refresh_tokens", "credential_version")
    op.drop_column("users", "membership_pending")
    for name in (
        "staff_membership_invites",
        "staff_account_reset_codes",
        "staff_account_sessions",
        "staff_accounts",
    ):
        op.drop_table(name)
