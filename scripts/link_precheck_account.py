"""Link a brain user to their PreCheck user id — the SSO account-link (onboarding).

A clinic that owns both products needs ONE row in `precheck_account_links` so its doctor's
brain login can open the PreCheck dashboard without a second login. This utility creates or
updates that row. Idempotent: re-running with the same pair is a no-op; re-running with a
new PreCheck id updates in place.

You must supply the PreCheck user's INTEGER id (from PreCheck's OWN users table) — it is not
derivable here (separate database). Find it on the PreCheck side (its users/admin listing).

Usage:
    uv run python scripts/link_precheck_account.py \
        --brain-email dra.demo@clinica.com.br --precheck-user-id 1

    # or by brain user id (UUID):
    uv run python scripts/link_precheck_account.py \
        --brain-user-id 1d3f... --precheck-user-id 1
"""

import argparse
import asyncio
import sys
from uuid import UUID

from sqlalchemy import select

from brain_api.core.database import async_session_factory
from brain_api.core.logging import get_logger, setup_logging
from brain_api.models import PrecheckAccountLink, User

setup_logging()
logger = get_logger(__name__)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Link a brain user to a PreCheck user id.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--brain-email", help="Brain user email")
    g.add_argument("--brain-user-id", help="Brain user id (UUID)")
    p.add_argument(
        "--precheck-user-id",
        type=int,
        required=True,
        help="PreCheck user INTEGER id (from PreCheck's users table)",
    )
    return p.parse_args(argv)


async def _resolve_user(session, args: argparse.Namespace) -> User | None:
    if args.brain_email:
        return await session.scalar(
            select(User).where(User.email == args.brain_email.lower())
        )
    try:
        uid = UUID(args.brain_user_id)
    except ValueError:
        print(f"[error] --brain-user-id is not a UUID: {args.brain_user_id}", file=sys.stderr)
        return None
    return await session.scalar(select(User).where(User.id == uid))


async def link(argv: list[str]) -> int:
    args = _parse_args(argv)
    # Capture primitives INSIDE the transaction; attributes expire on commit and a lazy
    # refresh outside the async context would raise. None means "abort with code 1".
    result: tuple[str, str, str, int] | None = None

    async with async_session_factory() as session, session.begin():
        user = await _resolve_user(session, args)
        if user is None:
            print("[error] brain user not found", file=sys.stderr)
            return 1
        if user.tenant_id is None:
            print(
                f"[error] {user.email} has no tenant (platform admin?) — cannot link",
                file=sys.stderr,
            )
            return 1

        # Reverse-uniqueness guard: is this PreCheck id already claimed by someone else?
        other = await session.scalar(
            select(PrecheckAccountLink).where(
                PrecheckAccountLink.precheck_user_id == args.precheck_user_id
            )
        )
        if other is not None and other.brain_user_id != user.id:
            print(
                f"[error] precheck_user_id={args.precheck_user_id} is already linked to a "
                f"different brain user ({other.brain_user_id})",
                file=sys.stderr,
            )
            return 1

        existing = await session.scalar(
            select(PrecheckAccountLink).where(
                PrecheckAccountLink.brain_user_id == user.id
            )
        )
        if existing is not None:
            existing.precheck_user_id = args.precheck_user_id
            existing.tenant_id = user.tenant_id
            action = "updated"
        else:
            session.add(
                PrecheckAccountLink(
                    brain_user_id=user.id,
                    precheck_user_id=args.precheck_user_id,
                    tenant_id=user.tenant_id,
                )
            )
            action = "created"

        result = (action, user.email, str(user.tenant_id), args.precheck_user_id)

    action, email, tenant_id, precheck_user_id = result
    logger.info(
        f"precheck_link_{action}",
        brain_email=email,
        precheck_user_id=precheck_user_id,
        tenant_id=tenant_id,
    )
    print(f"[ok] link {action}: {email} -> precheck user {precheck_user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(link(sys.argv[1:])))
