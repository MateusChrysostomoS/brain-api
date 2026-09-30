"""Guard for Stripe proof steps that MOVE MONEY (owner decision, 2026-09-29).

The brain-frontend Stripe account is a REAL one and the proofs may run on it.
Money-moving steps (add-product confirm=true, completing a payment, anything that
creates an invoice/charge or changes a real customer's subscription) run ONLY
against the owner-designated tenant + customer. Anything else aborts THAT step
(the plan continues); absent designation skips it. Never prints the env values.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

MONEY_MOVING_ENV = ("STRIPE_PROOF_TENANT_ID", "STRIPE_PROOF_CUSTOMER_ID")


@dataclass(frozen=True)
class ProofResult:
    name: str
    status: str  # "RAN" | "SKIP" | "ABORTED"
    reason: str


def run_money_step(
    name: str,
    target_tenant_id: str,
    target_customer_id: str,
    action: Callable[[], object],
    env: Mapping[str, str] | None = None,
) -> ProofResult:
    source = os.environ if env is None else env
    designated = {key: (source.get(key) or "") for key in MONEY_MOVING_ENV}
    if any(not value.strip() for value in designated.values()):
        return ProofResult(
            name,
            "SKIP",
            (
                "sem STRIPE_PROOF_TENANT_ID/STRIPE_PROOF_CUSTOMER_ID no ambiente "
                "(skip automatico; ver pendencias do dono)"
            ),
        )
    if (
        target_tenant_id != designated["STRIPE_PROOF_TENANT_ID"]
        or target_customer_id != designated["STRIPE_PROOF_CUSTOMER_ID"]
    ):
        return ProofResult(
            name,
            "ABORTED",
            (
                "alvo diferente do tenant/cliente designado pelo dono; "
                "passo abortado, nada foi chamado"
            ),
        )
    action()
    return ProofResult(name, "RAN", "alvo igual ao designado")
