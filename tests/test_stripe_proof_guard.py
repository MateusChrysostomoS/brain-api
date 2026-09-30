"""Tests for the money-moving proof guard (loaded by path: scripts/ is not a package)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "stripe_proof_guard.py"
_spec = importlib.util.spec_from_file_location("stripe_proof_guard", _PATH)
guard = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = guard
_spec.loader.exec_module(guard)

DESIGNATED = {"STRIPE_PROOF_TENANT_ID": "tenant-proof", "STRIPE_PROOF_CUSTOMER_ID": "cus_proof"}


def _calls():
    box = []
    return box, lambda: box.append("called")


def test_skips_when_designation_env_is_absent():
    box, action = _calls()
    result = guard.run_money_step("confirm", "tenant-proof", "cus_proof", action, env={})
    assert result.status == "SKIP" and box == []
    assert "STRIPE_PROOF_TENANT_ID" in result.reason


@pytest.mark.parametrize("missing", ["STRIPE_PROOF_TENANT_ID", "STRIPE_PROOF_CUSTOMER_ID"])
def test_skips_when_only_one_designation_var_is_set(missing):
    box, action = _calls()
    env = {k: v for k, v in DESIGNATED.items() if k != missing}
    assert (
        guard.run_money_step("confirm", "tenant-proof", "cus_proof", action, env=env).status
        == "SKIP"
    )
    assert box == []


def test_skips_when_a_designation_var_is_blank():
    box, action = _calls()
    env = {**DESIGNATED, "STRIPE_PROOF_CUSTOMER_ID": "  "}
    assert (
        guard.run_money_step("confirm", "tenant-proof", "cus_proof", action, env=env).status
        == "SKIP"
    )
    assert box == []


@pytest.mark.parametrize(
    ("tenant", "customer"),
    [
        ("tenant-other", "cus_proof"),  # wrong tenant, right customer
        ("tenant-proof", "cus_other"),  # right tenant, wrong customer
        ("tenant-other", "cus_other"),  # both wrong
        ("Tenant-Proof", "cus_proof"),  # case differs: not the same id
        ("tenant-proof ", "cus_proof"),  # trailing space: not the same id
        ("", "cus_proof"),
    ],
)
def test_aborts_that_step_when_target_is_not_the_designated_one(tenant, customer):
    box, action = _calls()
    result = guard.run_money_step("confirm", tenant, customer, action, env=DESIGNATED)
    assert result.status == "ABORTED" and box == []
    assert (
        "cus_proof" not in result.reason and "tenant-proof" not in result.reason
    )  # never echo the designated ids


def test_runs_only_on_the_exact_designated_pair():
    box, action = _calls()
    result = guard.run_money_step("confirm", "tenant-proof", "cus_proof", action, env=DESIGNATED)
    assert result.status == "RAN" and box == ["called"]


def test_an_aborted_step_does_not_stop_the_next_one():
    box, action = _calls()
    first = guard.run_money_step("a", "tenant-other", "cus_proof", action, env=DESIGNATED)
    second = guard.run_money_step("b", "tenant-proof", "cus_proof", action, env=DESIGNATED)
    assert (first.status, second.status) == ("ABORTED", "RAN") and box == ["called"]
