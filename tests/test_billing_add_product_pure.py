"""Pure helpers of the add-product flow: proration choice, period end, preview parsing."""

import pytest
from fastapi import HTTPException

from brain_api.services.billing import (
    AddProductCharge,
    parse_preview_charge,
    period_end_of,
    proration_for_items,
)

LIVE = {"id": "sub_1", "current_period_end": 1_702_592_000}  # 2023-12-14T22:13:20Z


def test_flat_items_are_invoiced_now_and_metered_only_is_not():
    assert proration_for_items([("price_pc_basic", "1")]) == "always_invoice"
    assert proration_for_items([("p1", None), ("p2", None), ("p3", None)]) == "none"
    assert proration_for_items([("p1", None), ("price_pix", "1")]) == "always_invoice"


def test_period_end_reads_the_subscription_or_falls_back_to_its_items():
    assert period_end_of({"current_period_end": 1_702_592_000}) == 1_702_592_000
    newer_shape = {
        "items": {
            "data": [{"current_period_end": 1_702_600_000}, {"current_period_end": 1_702_592_000}]
        }
    }
    assert period_end_of(newer_shape) == 1_702_592_000
    assert period_end_of({}) is None
    assert period_end_of({"current_period_end": True}) is None  # a bool is not a timestamp


def test_preview_with_top_level_proration_flag():
    from tests.billing_fakes import make_invoice

    assert parse_preview_charge(make_invoice(4500), LIVE) == AddProductCharge(
        "brl", 4500, None, None
    )


def test_preview_with_the_newer_parent_shape():
    from tests.billing_fakes import make_invoice

    preview = make_invoice(
        4000,
        lines={
            "has_more": False,
            "data": [
                {"amount": 4500, "parent": {"subscription_item_details": {"proration": True}}},
                {"amount": -500, "parent": {"subscription_item_details": {"proration": True}}},
            ],
        },
    )
    assert parse_preview_charge(preview, LIVE).amount_due_now_cents == 4000


def test_metered_only_addition_has_nothing_due_now():
    from tests.billing_fakes import make_invoice

    preview = make_invoice(9000, proration=False, billing_reason="subscription_cycle")
    charge = parse_preview_charge(preview, LIVE)
    assert charge.amount_due_now_cents == 0
    assert charge.next_invoice_cents == 9000
    assert charge.next_invoice_date == "2023-12-14"


@pytest.mark.parametrize(
    "preview",
    [
        {},
        {"currency": "brl"},
        {"currency": "brl", "lines": {"data": []}},
        {"lines": {"data": [{"amount": 1}]}},  # no currency
        {"currency": "brl", "lines": {"data": [{"amount": "12.50"}]}},
        {"currency": "brl", "lines": {"data": [{"amount": True}]}},
    ],
)
def test_an_unparseable_preview_fails_closed(preview):
    with pytest.raises(HTTPException) as exc:
        parse_preview_charge(preview, LIVE)
    assert (exc.value.status_code, exc.value.detail) == (502, "preview_unavailable")


def test_no_period_end_means_no_date_not_a_wrong_one():
    from tests.billing_fakes import make_invoice

    preview = make_invoice(100)
    assert parse_preview_charge(preview, {}).next_invoice_date is None
