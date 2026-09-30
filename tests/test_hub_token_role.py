"""TASK-020: the hub token carries the acting user's role (token half, no HTTP)."""

import pytest

from brain_api.config import get_settings
from brain_api.core.security import decode_hub_token
from tests.hub_tokens import forge_hub_token, make_hub_token, tamper_hub_token, unsigned_hub_token


@pytest.mark.parametrize(
    ("role", "is_owner", "is_manager"),
    [
        ("manager", False, True),
        ("doctor", True, True),
        ("doctor", False, False),
        ("secretary", False, False),
    ],
)
def test_hub_token_carries_role_claims_when_given(role, is_owner, is_manager):
    claims = decode_hub_token(make_hub_token(role=role, is_owner=is_owner, is_manager=is_manager))
    assert claims is not None
    assert claims["role"] == role
    assert claims["is_owner"] is is_owner
    assert claims["is_manager"] is is_manager


def test_hub_token_without_role_keeps_the_old_shape():
    claims = decode_hub_token(make_hub_token(role=None, is_owner=True, is_manager=True))
    assert claims is not None
    for name in ("role", "is_owner", "is_manager"):
        assert name not in claims


def test_hub_token_still_has_no_tenant_id_claim_and_keeps_scope():
    claims = decode_hub_token(make_hub_token(role="manager", is_manager=True))
    assert claims is not None
    assert "tenant_id" not in claims
    assert claims["scope"] == "secretaria_hub"


def test_hub_token_ttl_is_unchanged():
    claims = decode_hub_token(make_hub_token(role="manager", is_manager=True))
    assert claims is not None
    assert claims["exp"] - claims["iat"] == get_settings().HUB_TOKEN_EXPIRE_MINUTES * 60


def test_payload_swapped_with_old_signature_is_rejected():
    token = make_hub_token(role="secretary")
    assert decode_hub_token(tamper_hub_token(token, role="manager", is_manager=True)) is None


def test_alg_none_token_is_rejected():
    assert decode_hub_token(unsigned_hub_token(role="manager", is_manager=True)) is None


def test_token_signed_with_another_key_is_rejected():
    forged = forge_hub_token(
        {"role": "manager", "is_manager": True}, secret="not-the-real-secret-key-not-the-real"
    )
    assert decode_hub_token(forged) is None
