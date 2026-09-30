"""`_stripe_post_or_raise` + `classify_add_product_error` (TASK C, spec §5.5 step 7)."""

import httpx
import pytest
from fastapi import HTTPException

from brain_api.services import billing as billing_service
from brain_api.services.billing import (
    StripeApiError,
    _stripe_post_or_raise,
    classify_add_product_error,
)
from tests.billing_fakes import install_fake_settings


class _Resp:
    def __init__(self, status_code: int, body) -> None:
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _install_client(monkeypatch, *, response=None, raises=None) -> dict:
    captured: dict = {}

    class _Client:
        def __init__(self, **kwargs) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> bool:
            return False

        async def post(self, path, data=None, auth=None, headers=None):
            captured.update(path=path, data=data, auth=auth, headers=headers)
            if raises is not None:
                raise raises
            return response

    monkeypatch.setattr(billing_service.httpx, "AsyncClient", _Client)
    return captured


async def test_success_returns_the_body_and_sends_the_idempotency_key(monkeypatch):
    install_fake_settings(monkeypatch)
    captured = _install_client(monkeypatch, response=_Resp(200, {"id": "sub_1"}))
    body = await _stripe_post_or_raise("/v1/subscriptions/sub_1", {"a": "b"}, idempotency_key="k-1")
    assert body == {"id": "sub_1"}
    assert captured["headers"] == {"Idempotency-Key": "k-1"}
    assert captured["auth"] == ("sk_test_fake", "")
    assert captured["path"] == "/v1/subscriptions/sub_1"


async def test_error_response_carries_stripe_fields_and_no_payload(monkeypatch):
    install_fake_settings(monkeypatch)
    _install_client(
        monkeypatch,
        response=_Resp(
            402, {"error": {"code": "card_declined", "type": "card_error", "message": "Declined."}}
        ),
    )
    with pytest.raises(StripeApiError) as exc:
        await _stripe_post_or_raise("/v1/subscriptions/sub_1", {"items[0][price]": "price_x"})
    err = exc.value
    assert (err.status_code, err.code, err.error_type, err.message) == (
        402,
        "card_declined",
        "card_error",
        "Declined.",
    )
    assert "price_x" not in str(err)  # the request payload never travels in the exception


async def test_a_non_json_error_body_still_raises_a_stripe_api_error(monkeypatch):
    install_fake_settings(monkeypatch)
    _install_client(monkeypatch, response=_Resp(502, ValueError("<html>")))
    with pytest.raises(StripeApiError) as exc:
        await _stripe_post_or_raise("/v1/x", {})
    assert exc.value.status_code == 502 and exc.value.code is None


async def test_network_failure_is_502_stripe_unavailable(monkeypatch):
    install_fake_settings(monkeypatch)
    _install_client(monkeypatch, raises=httpx.ConnectError("boom"))
    with pytest.raises(HTTPException) as exc:
        await _stripe_post_or_raise("/v1/x", {})
    assert (exc.value.status_code, exc.value.detail) == (502, "stripe_unavailable")


async def test_missing_key_is_503_billing_not_configured(monkeypatch):
    install_fake_settings(monkeypatch, STRIPE_SECRET_KEY="")
    with pytest.raises(HTTPException) as exc:
        await _stripe_post_or_raise("/v1/x", {})
    assert (exc.value.status_code, exc.value.detail) == (503, "billing_not_configured")


@pytest.mark.parametrize(
    ("status_code", "code", "error_type", "message", "expected"),
    [
        (402, "card_declined", "card_error", "Your card was declined.", (402, "payment_failed")),
        (402, "expired_card", "card_error", None, (402, "payment_failed")),
        (
            402,
            "subscription_payment_intent_requires_action",
            "card_error",
            None,
            (409, "payment_action_required"),
        ),
        (402, "authentication_required", "card_error", None, (409, "payment_action_required")),
        (
            400,
            None,
            "invalid_request_error",
            "This customer has no attached payment source or default payment method.",
            (409, "payment_method_required"),
        ),
        (
            409,
            "idempotency_key_in_use",
            "idempotency_error",
            None,
            (409, "add_product_in_progress"),
        ),
        (
            400,
            "parameter_unknown",
            "invalid_request_error",
            "Received unknown parameter",
            (502, "stripe_error"),
        ),
        (500, None, "api_error", None, (502, "stripe_error")),
    ],
)
def test_classification(status_code, code, error_type, message, expected):
    assert (
        classify_add_product_error(StripeApiError(status_code, code, error_type, message))
        == expected
    )
