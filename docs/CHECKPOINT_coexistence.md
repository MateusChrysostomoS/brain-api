# CHECKPOINT — WhatsApp Coexistence onboarding (brain-api slice)

> See `docs/CHECKPOINT_onboarding_multiprofessional.md` for the onboarding state-machine
> foundation this round builds on top of, and
> `docs/GUIA_CREDENCIAIS_META_EMBEDDED_SIGNUP.md` for the Meta-panel setup steps
> (webhook fields, verify token, Task 0's feature-type research) this round added. Full
> API/data contract detail lives in `CONTRACTS.md` §7 (settings) and §16.2 (endpoints,
> error_code vocabulary) — this file is the "what landed where and what's still pending"
> record for the brain-api half of "WhatsApp Coexistence onboarding".

Validated 2026-08-09 (`uv run python -m pytest -q` equivalent — App Control blocked the
venv's `python.exe`, ran via the base uv `cpython-3.12` interpreter with
`PYTHONPATH=src;.venv/Lib/site-packages` instead → **429 passed**, 5 pre-existing
`StarletteDeprecationWarning`s unrelated to this round, ~4m56s). This is the brain-api
slice of the Coexistence onboarding feature: WhatsApp Coexistence lets a clinic connect a
number it ALREADY uses in the WhatsApp Business app, instead of provisioning a brand-new
number — a second onboarding path alongside the existing standard flow.

## What landed this round

- **New setting** — `config.py`: `META_ES_COEXISTENCE_FEATURE_TYPE: str = "whatsapp_business_app_onboarding"`
  (default matches Meta's own documented value), in the "Onboarding / multi-professional"
  block next to `META_ES_CONFIG_ID`. Not a secret — same echo-back convention as
  `META_ES_CONFIG_ID`.
- **New schema field** — `schemas/onboarding.py::EmbeddedSignupOut.coexistence_feature_type:
  str | None`. Independent of `configured` (which still requires only `app_id` AND
  `config_id`) — the frontend reads this field separately to decide whether to offer "já
  uso este número no WhatsApp Business".
- **Endpoint wiring** — `api/onboarding.py::_embedded_signup_out` now fills
  `coexistence_feature_type=settings.META_ES_COEXISTENCE_FEATURE_TYPE or None`, so
  `GET /doctor/onboarding`'s `embedded_signup` block carries it read-only, same pattern
  as `app_id`/`config_id`.
- **`services/meta_graph.py::subscribe_app_to_waba(waba_id, access_token)`** (new
  function) — `POST {META_GRAPH_BASE_URL}/{waba_id}/subscribed_apps`, subscribing this
  app to the client's WABA webhooks (required for both the standard and the Coexistence
  flow — Meta will not deliver `messages`/`smb_message_echoes`/`history`/
  `smb_app_state_sync` events for a WABA the app isn't subscribed to). Same
  `_TIMEOUT_SECONDS`/`httpx.AsyncClient(base_url=...)` pattern as
  `exchange_code_for_token`; `access_token` sent ONLY as a Bearer `Authorization` header,
  never a query param, never logged. Success = HTTP 200 (Meta returns
  `{"success": true}`); any `RequestError` or non-200 logs `meta_waba_subscribe_failed`
  (with `upstream_status` when available) and returns `False`. Never raises. Idempotent
  on Meta's side — this function does not cache.
- **`api/onboarding.py::post_attempt` wiring** — between a successful
  `exchange_code_for_token` and the `connect_whatsapp` call: when BOTH `payload.waba_id`
  and the exchanged `access_token` are truthy, calls `subscribe_app_to_waba`; a `False`
  return records a `fail` attempt with `error_code="waba_subscribe_failed"` (same
  `_record_fail` shape as the sibling `token_exchange_failed`/`phone_number_conflict`/
  `secretaria_connection_failed` branches — NOT fail-soft, a hard gate on the pass path).
  When either input is missing (no `waba_id`, or no `access_token` because `code` was
  absent or the exchange itself failed earlier), the subscribe step is skipped and logged
  as `meta_waba_subscribe_skipped` (booleans only — never logs the token) — this
  preserves the pre-existing "attempt with no code" path unchanged.
- **Contract vocabulary** (`CONTRACTS.md` §7, §16.2) — `META_ES_COEXISTENCE_FEATURE_TYPE`
  documented in the settings table; the `POST /doctor/onboarding/attempts` row now lists
  the full `error_code` vocabulary explicitly: `token_exchange_failed`,
  `waba_subscribe_failed`, `phone_number_conflict`, `secretaria_connection_failed`
  (backend-recorded) plus `no_phone_number_id` (FRONTEND-supplied — sent when the Meta
  Embedded Signup flow completes with no `phone_number_id` in its payload; no backend
  code change needed, it is just a normal `fail` attempt through the existing endpoint).

## Tests added (`tests/test_onboarding_endpoints.py`)

- `test_get_onboarding_embedded_signup_configured_when_both_ids_set` — updated (the
  monkeypatched `SimpleNamespace` needed the new attribute).
- `test_get_onboarding_shape_default_state` — updated (exact-dict assertion now includes
  `coexistence_feature_type`).
- `test_get_onboarding_coexistence_feature_type_set` — GET surfaces a non-empty setting
  value end to end.
- `test_subscribe_app_to_waba_success` / `_non_200_returns_false` / `_network_error_returns_false`
  — dedicated client tests (fake httpx, no network), same style as the existing
  `meta_graph.exchange_code_for_token` tests. The non-200 test also monkeypatches
  `meta_graph.logger.warning` directly and asserts the token string never appears in the
  captured call args/kwargs — `caplog` cannot see structlog's `PrintLoggerFactory` output
  in this codebase (it doesn't route through stdlib `logging`), so logger-call inspection
  is the meaningful equivalent here.
- `test_attempt_pass_waba_subscribe_failure_records_fail` — router-level: a failing
  subscribe records `error_code="waba_subscribe_failed"` and `connect_whatsapp` is
  asserted to never be called; also monkeypatches `onboarding_api.logger.info` and
  asserts the token never appears in any captured call.
- `test_attempt_pass_subscribe_success_then_connects` — subscribe succeeds, then
  `connect_whatsapp` still runs and the attempt reaches `conectado` normally.
- `test_attempt_pass_missing_waba_id_skips_subscribe` — no `waba_id` in the payload →
  `subscribe_app_to_waba` is asserted to never be called, and the attempt proceeds
  through `connect_whatsapp` unchanged (the pre-existing "attempt without a waba_id" path
  stays intact).
- All pre-existing `test_onboarding_endpoints.py` / `test_test_window.py` tests remain
  green unmodified except the two `embedded_signup`-shape ones listed above.

## Status

**BUILT, tests green, NOT deployed.** No migration — this round touches only settings,
schemas, and service/endpoint logic, no new/changed database columns.

## External dependencies (nothing left to build in this repo's code)

- **EasyPanel**: `META_ES_COEXISTENCE_FEATURE_TYPE` is not set in any deployed env yet
  (code default `"whatsapp_business_app_onboarding"` applies until it is). No env change
  is strictly REQUIRED for the default to work — only needed if a future value ever
  differs from Meta's documented default.
- **Meta panel**: the webhook fields (`messages`, `smb_message_echoes`, `history`,
  `smb_app_state_sync`) and the verify token (secretarIA's `META_VERIFY_TOKEN`) need to be
  configured once, manually, in App Dashboard → WhatsApp → Configuration — documented in
  `docs/GUIA_CREDENCIAIS_META_EMBEDDED_SIGNUP.md` (Parte A, seção 3, added this round).
  This is a one-time APP-level configuration, distinct from `subscribe_app_to_waba`
  (which subscribes the app to each individual CLIENT's WABA at connection time).
- **Live validation pending**: no real, Meta-eligible WhatsApp number has exercised this
  path yet (Tech Provider Program approval + a real Embedded Signup Configuration ID are
  still outstanding — see `docs/GUIA_CREDENCIAIS_META_EMBEDDED_SIGNUP.md`). Everything
  here is validated against fake-httpx unit/integration tests, not a live Meta call.
- **brain-frontend**: the `extras.featureType`/`FB.login()` wiring that actually launches
  the Coexistence flow, and consuming `embedded_signup.coexistence_feature_type` to
  decide whether to offer the "já uso este número" option, are out of this repo's scope
  (per the sensitive-repos convention — no code edits made here beyond brain-api).

## Pendências

- Live end-to-end test once Tech Provider Program approval + a real Configuration ID
  exist (same pre-existing blocker as the standard Embedded Signup flow, not new to this
  round).
- brain-frontend's own Coexistence-flow implementation (separate repo/round).
- No migration, no deploy performed as part of this round.

## Deviations from the prompt

None identified — the four asks (setting/schema/endpoint field, `subscribe_app_to_waba` +
`post_attempt` wiring, contract vocabulary, docs) were all implemented as specified, with
no scope changes discovered while building.

---

**Data:** 2026-08-09  
**Status:** BUILT, testes verdes, NÃO deployado.
