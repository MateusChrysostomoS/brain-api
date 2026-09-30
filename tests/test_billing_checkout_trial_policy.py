"""The authenticated checkout follows the trial policy of `_trial_days_for` (TASK C, D7):
trial only for plans that carry secretarIA — a PreCheck checkout charges at once."""

import pytest

from tests.test_billing import _install_fake_stripe_httpx
from tests.test_rbac import OWNER_B_EMAIL, OWNER_B_PASSWORD, _bearer, _token


async def _checkout(client, plan: str):
    token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    return await client.post("/billing/checkout", headers=_bearer(token), json={"plan": plan})


@pytest.mark.parametrize("plan", ["precheck_start", "precheck_basic", "precheck_advanced"])
async def test_a_precheck_checkout_carries_no_trial_even_when_a_trial_is_configured(
    client, monkeypatch, plan
):
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"url": "https://checkout.stripe.test/x"}, trial_period_days=75
    )
    resp = await _checkout(client, plan)
    assert resp.status_code == 200, resp.text
    assert "subscription_data[trial_period_days]" not in captured["data"]
    assert "custom_text[submit][message]" not in captured["data"]


@pytest.mark.parametrize("plan", ["secretaria_basico", "complete_clinic_combo"])
async def test_a_secretaria_checkout_still_carries_the_trial(client, monkeypatch, plan):
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"url": "https://checkout.stripe.test/x"}, trial_period_days=75
    )
    resp = await _checkout(client, plan)
    assert resp.status_code == 200, resp.text
    assert captured["data"]["subscription_data[trial_period_days]"] == "75"
    assert captured["data"]["custom_text[submit][message]"]
