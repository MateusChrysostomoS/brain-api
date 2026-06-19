"""SSO handoff schemas (CONTRACTS.md §5).

The response of POST /sso/precheck/token. `token` is a PreCheck-compatible JWT (the
portal stores it as PreCheck's `precheck_token` and redirects into the PreCheck app). No
brain identity/secret is exposed — only the opaque handoff token + its lifetime.
"""

from pydantic import BaseModel, ConfigDict


class PrecheckSsoTokenResponse(BaseModel):
    """A minted PreCheck session token + how long it is valid (seconds)."""

    model_config = ConfigDict(extra="ignore")

    token: str
    token_type: str = "bearer"
    expires_in: int
