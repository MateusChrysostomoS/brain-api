"""Replay of REAL (sanitized) Stripe subscription objects through the derivation
(TASK C, spec §5.15). Dormant until fixtures are captured during the proof."""

import json
import pathlib
from types import SimpleNamespace

import pytest

from brain_api.services import billing as billing_service

FIXTURES = sorted((pathlib.Path(__file__).parent / "fixtures" / "stripe").glob("*.json"))


@pytest.mark.skipif(
    not FIXTURES, reason="no captured Stripe fixtures yet (see fixtures/stripe/README.txt)"
)
@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_a_captured_subscription_derives_the_expected_families(path, monkeypatch):
    case = json.loads(path.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        billing_service,
        "get_settings",
        lambda: SimpleNamespace(STRIPE_PRICE_MAP=json.dumps(case["price_map"])),
    )
    state = billing_service._state_from_subscription(case["subscription"])
    assert state is not None, case["note"]
    expect = case["expect"]
    assert state["plan"] == expect["plan"]
    assert state["precheck_plan"] == expect["precheck_plan"]
    assert state["precheck_enabled"] is expect["precheck"]
    assert state["secretaria_enabled"] is expect["secretaria"]


def test_the_replay_harness_itself_is_wired():
    """Runs even with no fixtures: the loader and the derivation are importable together."""
    assert callable(billing_service._state_from_subscription)
    assert isinstance(FIXTURES, list)
