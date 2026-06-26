"""Create (or promote) a platform-admin user — full access to the /admin/* surface.

A platform admin is `role="admin"` with `tenant_id = NULL` (see models/user.py). Every
/admin/* route is gated by `require_role("admin")` (api/admin.py), so this account can
reach the entire admin plane. It deliberately has NO tenant, so it is rejected from the
per-clinic doctor routes (deps.require_tenant -> 409, deps.require_doctor -> 403) — that is
the platform's RBAC model, not a bug.

Idempotent: if the email already exists, the user is PROMOTED to admin (role -> admin,
tenant detached) and — only if --password is given — the password is reset. Re-running with
the same args is safe.

Usage:
    uv run python scripts/create_admin.py \
        --email you@brain.co --password "a-strong-pass" --name "You"

    # promote an existing user to admin without touching their password:
    uv run python scripts/create_admin.py --email existing@brain.co
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from brain_api.core.database import async_session_factory
from brain_api.core.logging import get_logger, setup_logging
from brain_api.core.security import hash_password
from brain_api.models import User
from brain_api.models.user import ROLE_ADMIN

setup_logging()
logger = get_logger(__name__)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create or promote a platform-admin user.")
    p.add_argument("--email", required=True, help="Admin login email")
    p.add_argument(
        "--password",
        help="Admin password (max 72 bytes — bcrypt). Required when creating a new user; "
        "optional when promoting an existing one.",
    )
    p.add_argument("--name", default="Platform Admin", help="Display name (new users only)")
    return p.parse_args(argv)


async def create_admin(argv: list[str]) -> int:
    args = _parse_args(argv)
    email = args.email.strip().lower()  # stored lower-cased (see models/user.py)

    if args.password is not None and len(args.password.encode("utf-8")) > 72:
        print("[error] password exceeds 72 bytes (bcrypt limit)", file=sys.stderr)
        return 1

    # Capture what we print INSIDE the transaction — attributes expire on commit and a lazy
    # refresh outside the async context would raise. (Same pattern as link_precheck_account.)
    result: tuple[str, str] | None = None

    async with async_session_factory() as session, session.begin():
        existing = await session.scalar(select(User).where(User.email == email))

        if existing is not None:
            existing.role = ROLE_ADMIN
            existing.tenant_id = None  # platform admins are tenant-less
            if args.password is not None:
                existing.password_hash = hash_password(args.password)
            result = ("promoted", email)
        else:
            if not args.password:
                print("[error] --password is required to create a new user", file=sys.stderr)
                return 1
            session.add(
                User(
                    email=email,
                    name=args.name,
                    password_hash=hash_password(args.password),
                    role=ROLE_ADMIN,
                    tenant_id=None,
                )
            )
            result = ("created", email)

    action, email = result
    logger.info(f"admin_{action}", email=email, role=ROLE_ADMIN)
    print(f"[ok] admin {action}: {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(create_admin(sys.argv[1:])))
