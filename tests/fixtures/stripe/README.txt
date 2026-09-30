Stripe subscription objects captured during the TASK C proof (real or test account)
(docs/CHECKPOINT_billing_add_product.md §6), sanitized: real ids replaced by
`sub_fixture_*`, `cus_fixture_*`, `price_fixture_*`; no email, no card data, no payload
secrets. One JSON per case: {"note", "price_map", "subscription", "expect"}.
Replayed by tests/test_billing_stripe_fixtures.py.
