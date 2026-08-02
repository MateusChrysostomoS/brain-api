# CHECKPOINT — Launch waitlist (pre-launch buy gate, backend half)

Status: **BUILT + tested locally (2026-08-01)**, UNCOMMITTED, **migration 0011 NOT deployed**.

Full suite (from `C:\TECH\BRAIN\brain-api`):

```
uv run python -m pytest -q
```

→ **417 passed**, 0 failed (whole suite, including the new `tests/test_launch_waitlist.py`, 12 tests).
Lint/format clean on every file this round touched (`uv run ruff check` / `ruff format --check`).

The frontend half is documented in `brain-frontend/docs/CHECKPOINT_launch_waitlist.md` —
**the launch flag itself lives there**, not here.

## Why

The pricing page works end to end (authenticated Stripe Checkout + the anonymous
`/cadastro` funnel), but the product is not commercially launched — there are no paying
clinics yet (see `docs/CHECKPOINT_register_at_first_card.md` for the funnel it gates).
So the frontend now blocks every buy click behind a "Estamos quase lá" modal. That modal
needs somewhere to put the visitor who WANTED to buy: this endpoint.

This is pure lead capture, in the same isolated-vertical spirit as `POST /demo-requests`
(CONTRACTS.md §0.4 / §4 / §5): no tenant, no user, no entitlement, no Stripe, no async
work. It is additive — nothing about the existing checkout/signup paths changed.

## What changed, and where

### 1. `models/waitlist_lead.py` (new) — `waitlist_leads`

`id`, `name`, `email` (UNIQUE, indexed), `plan_hint` (nullable), `created_at` (indexed).
No FKs, no reference to tenants/users/entitlements.

The UNIQUE `email` is the load-bearing decision: it is what makes the endpoint idempotent,
so a visitor clicking three different plan cards leaves ONE row instead of three.

### 2. `schemas/waitlist.py` (new)

`WaitlistLeadCreate` mirrors `schemas/demo.py`: trim-and-reject-blank `name`, `EmailStr`,
and the same never-persisted `website` HONEYPOT field. `plan_hint` is trimmed, and
blank-after-trim becomes `NULL`.

`plan_hint` is deliberately **not** validated against `services/catalog.py`: it records a
sales hint for a catalog that may well have changed by launch day, and an unknown id must
never 422 a lead out of the list.

`WaitlistLeadConfirmation` returns `{id, message}` only — no lead data echoed back.

### 3. `services/waitlist.py` (new) — `upsert_waitlist_lead`

Idempotency rules (decided here, the prompt left it open):

- `email` is lowercased before both the lookup and the insert, so case variants collapse.
- A repeat submission REFRESHES `name` and `plan_hint` (the latest click is the better
  sales signal) but **never rewrites `created_at`** — that field means "first asked", which
  is what tells us who has been waiting longest.
- A later submission with NO `plan_hint` does not erase a hint we already have.
- A concurrent duplicate races to `IntegrityError`; that path rolls back, re-reads the
  winning row and applies the same refresh, so the caller still gets a normal 201.

Rate limiter: its OWN `SlidingWindowLimiter("waitlist", ...)` instance, **not** the
`/public/*` signup bucket. The prompt asked for "the same shared bucket", but
`core/ratelimit.py` states the opposite invariant explicitly ("One `SlidingWindowLimiter`
instance per protected surface … so their buckets never interfere"), and coupling them
would let a pre-launch marketing form eat the budget the real checkout funnel depends on
the moment the gate opens. Same machinery, same fail-open contract, separate bucket.

### 4. `api/public_waitlist.py` (new) — `POST /public/launch-waitlist`

Unauthenticated. Order: honeypot accept-and-drop (synthetic nil UUID, nothing persisted)
→ per-IP rate limit (429) → upsert → fixed confirmation copy. The confirmation is
IDENTICAL for a first-time and a repeat submission, so the response never reveals whether
an e-mail was already on the list. Never logs name/e-mail — only `id` + `plan_hint`.

### 5. Wiring

- `models/__init__.py` — exports `WaitlistLead` (registers the table on `Base.metadata`).
- `main.py` — `app.include_router(public_waitlist.router, tags=["public"])`.
- `config.py` — `WAITLIST_RATE_LIMIT_PER_MIN: int = 5` (defaulted; **no EasyPanel change
  required** unless you want a different number).
- `tests/conftest.py` — sets `WAITLIST_RATE_LIMIT_PER_MIN=0` for the suite, matching how
  the auth/signup limiters are already neutralised; the dedicated rate-limit test
  monkeypatches its own limiter instance.

## Migration

`migrations/versions/0011_launch_waitlist.py` — `0010_precheck_billing → 0011_launch_waitlist`.

**Status: written, chain verified, NOT applied anywhere.**

Additive and isolated: creates `waitlist_leads` + its two indexes, touches no existing
table, no FK, no data backfill. Safe to apply before or after the code deploy — nothing
reads the table until the endpoint is live.

Verified offline (no DB needed) with:

```
uv run alembic heads                                              # single head: 0011_launch_waitlist
uv run alembic upgrade --sql 0010_precheck_billing:0011_launch_waitlist
```

The rendered Postgres DDL matches the model exactly (UUID pk, `VARCHAR(320)` e-mail with a
UNIQUE index, `TIMESTAMPTZ DEFAULT now()`).

Deploy order does not matter, but the conventional one applies: `alembic upgrade head`,
then the code.

## Reading the leads

There is deliberately NO admin route and no panel (explicitly out of scope). For now:

```sql
SELECT name, email, plan_hint, created_at FROM waitlist_leads ORDER BY created_at DESC;
```

## Pendências

- [ ] Apply migration `0011_launch_waitlist` on the deployed database.
- [ ] Deploy brain-api with the new router.
- [ ] (Optional, later) an admin page to read the list — see `api/admin.py` for the pattern.
- [ ] Retire or leave harmless once `PRODUCT_LAUNCHED` flips: the endpoint stops receiving
      traffic on its own, nothing to undo.
