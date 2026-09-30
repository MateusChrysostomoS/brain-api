"""Test helpers that mint/forge secretarIA hub tokens (TASK-020).

`make_hub_token` goes through the REAL `create_hub_token`; the other three build the
hostile shapes a role gate must survive (a wrong signature, a swapped payload, `alg: none`).
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from jose import jwt as jose_jwt

from brain_api.config import get_settings
from brain_api.core.security import ALGORITHM, HUB_TOKEN_SCOPE, create_hub_token


def make_hub_token(
    *,
    role: str | None = "doctor",
    is_owner: bool = False,
    is_manager: bool = False,
    tenant_id: str | None = None,
    actor_user_id: str = "user-1",
    professional_id: str | None = None,
) -> str:
    """A hub token exactly as brain-api mints it. `role=None` is the pre-TASK-020 shape."""
    return create_hub_token(
        tenant_id=tenant_id or str(uuid4()),
        actor_user_id=actor_user_id,
        professional_id=professional_id,
        role=role,
        is_owner=is_owner,
        is_manager=is_manager,
    )


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _base_claims() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "sub": str(uuid4()),
        "scope": HUB_TOKEN_SCOPE,
        "act": "user-1",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }


def forge_hub_token(
    overrides: dict[str, Any] | None = None,
    *,
    secret: str | None = None,
    drop: tuple[str, ...] = (),
) -> str:
    """A validly-signed hub token with arbitrary claims (or signed with another key)."""
    claims = _base_claims()
    claims.update(overrides or {})
    for key in drop:
        claims.pop(key, None)
    return jose_jwt.encode(claims, secret or get_settings().SECRET_KEY, algorithm=ALGORITHM)


def tamper_hub_token(token: str, **claim_changes: Any) -> str:
    """Swap claims in the payload but KEEP the original signature (signature no longer matches)."""
    header, payload, signature = token.split(".")
    padded = payload + "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    claims.update(claim_changes)
    return ".".join((header, _b64(claims), signature))


def unsigned_hub_token(**claims: Any) -> str:
    """`alg: none` with an empty signature — the classic algorithm-confusion forgery."""
    body: dict[str, Any] = {
        "sub": str(uuid4()),
        "scope": HUB_TOKEN_SCOPE,
        "act": "user-1",
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
    }
    body.update(claims)
    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(body)}."
