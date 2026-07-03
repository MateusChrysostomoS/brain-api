# Mesh secret rotation — zero-downtime procedures

The mesh runs on three long-lived shared secrets (CONTRACTS.md §0/§7). Each now has a
`*_PREVIOUS` companion accepted **for verification only** during a rotation window, so
the two sides of an invariant can be flipped one deploy at a time without breaking SSO
or the internal data paths. Minting/sending ALWAYS uses the current value.

Generate every new value with `openssl rand -hex 64` (SECRET_KEY) or
`python -c "import secrets; print(secrets.token_hex(32))"` (API keys/tokens). Never
paste a value into a log, ticket, or commit.

## 1. `SECRET_KEY` (brain-api ↔ PreCheck — JWTs + SSO handoff)

Verifiers: brain-api (its own user JWTs and hub tokens, with `SECRET_KEY_PREVIOUS`
fallback) and PreCheck (the minted SSO token, validated with PreCheck's own single
`SECRET_KEY` — PreCheck has **no** previous-key fallback; do not assume one).

1. On **brain-api**: set `SECRET_KEY=<new>`, `SECRET_KEY_PREVIOUS=<old>`. Deploy.
   - brain-api now mints with the new key and still accepts every token minted with
     the old one, so no logged-in user or open hub session breaks.
2. On **PreCheck**: set `SECRET_KEY=<new>`. Deploy **immediately after** step 1.
   - The invariant (byte-identical SECRET_KEY) is restored the moment both deploys
     land. In the seconds between them, a freshly minted SSO token (60 min TTL, minted
     with the new key) would be rejected by a PreCheck still holding the old key — the
     portal retry (re-clicking the product card) succeeds as soon as PreCheck flips.
     Already-issued PreCheck sessions are unaffected (PreCheck signs its own tokens
     with the same variable and old sessions were signed with the old key — flip
     PreCheck during a quiet window if that matters; their TTL is 60 min).
3. After `ACCESS_TOKEN_EXPIRE_MINUTES` + the longest hub/SSO TTL (≈2h worst case):
   clear `SECRET_KEY_PREVIOUS=` on brain-api. Deploy. Rotation complete.

Note: refresh tokens are opaque DB rows, not JWTs — they survive a SECRET_KEY
rotation untouched, which is exactly why the portal recovers seamlessly (a refresh
mints a new access token under the new key).

## 2. The brain ↔ secretaria pair key (`SECRETARIA_API_KEY` = `INTERNAL_API_KEY`)

One secret, both directions: brain-api → secretaria `/internal/*` (doctor data reads)
and secretaria → brain-api `/internal/*` (hub-token introspection + entitlement
summaries). Verifiers on BOTH sides accept their `*_PREVIOUS` during the window.

1. On **secretaria**: set `INTERNAL_API_KEY=<new>`, `INTERNAL_API_KEY_PREVIOUS=<old>`.
   Deploy. (secretaria now *sends* the new key to brain-api and *accepts* both.)
2. On **brain-api**: set `SECRETARIA_API_KEY=<new>`,
   `SECRETARIA_API_KEY_PREVIOUS=<old>`. Deploy. (Same on its side.)
   - Order between 1 and 2 does not matter: whichever deploys first sends a key the
     other still accepts via its previous-slot or hasn't rotated away from yet.
3. Once both run the new value: clear both `*_PREVIOUS` vars. Two quick deploys.

## 3. `SECRETARIA_ADMIN_TOKEN` (brain-api → secretaria `/admin/*`, X-Admin-Token)

Verifier: secretaria only (`ADMIN_TOKEN`, with `ADMIN_TOKEN_PREVIOUS` fallback).

1. On **secretaria**: set `ADMIN_TOKEN=<new>`, `ADMIN_TOKEN_PREVIOUS=<old>`. Deploy.
2. On **brain-api**: set `SECRETARIA_ADMIN_TOKEN=<new>`. Deploy.
3. On **secretaria**: clear `ADMIN_TOKEN_PREVIOUS=`. Deploy. Complete.

## Rules that apply to every rotation

- A `*_PREVIOUS` slot is a **window**, not a second permanent key: clearing it is part
  of the procedure, not optional hygiene.
- Verify-only: nothing ever signs/sends with a previous value — if you see a service
  *sending* an old key after its flip deploy, the deploy did not actually restart it.
- Secrets never appear in logs (structlog redaction + the "never log the candidate"
  rule in every verifier). Compare with `secrets.compare_digest` / `hmac.compare_digest`
  only.
- After each step, smoke-check the affected path: portal login + PreCheck card (§1),
  doctor appointments page + hub open (§2), admin secretaria panel (§3).
