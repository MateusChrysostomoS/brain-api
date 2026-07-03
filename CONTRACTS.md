# brain-api — API & Data Contracts (Phase 1)

> **Status:** authoritative. Phase 2 (backend) and Phase 3 (frontend) build against
> THIS document. If an implementation detail disagrees with this file, this file wins
> until it is amended here first.

`brain-api` is the **identity authority + BFF** for the Brain platform. It owns
`users`, `tenants`, `entitlements`, and `demo_requests` inside its own **`brain`
database** in the shared Postgres service. It faces the browser (the unified portal in
`brain-frontend`). In Phase 1, `secretaria` and `precheck` were internal-only and not
called from here. **The RBAC round (§11–§12) extends this:** brain-api now exposes
role-gated `/admin/*` and `/doctor/*` portals and acts as a **read proxy into PreCheck**
(forwarding the caller's brain JWT). It also calls **`secretaria`** two ways: the admin
connection (`X-Admin-Token`, §11.2) and the **doctor appointments/patients data path**
(`X-Internal-Api-Key`, §12.1). No browser ever calls `precheck`/`secretaria` directly.

Stack (mirrors `secretarIA`): Python 3.12, FastAPI, async SQLAlchemy 2.0 (asyncpg),
Alembic (async env), pydantic v2 / pydantic-settings, structlog, uv + hatchling.
Auth adds **python-jose** (JWT, HS256) and **passlib[bcrypt]** (passwords) per the
`auth-jwt-multitenant` skill. No Redis/arq — there is no off-request async work in this
task.

---

## 0. Cross-service boundaries & decisions (read first)

1. **brain-api is its own identity authority.** Its JWT follows `auth-jwt-multitenant`:
   HS256, signed with `SECRET_KEY`, claims `sub` / `tenant_id` / `role` / `iat` / `exp`.
   `sub` is the brain **user UUID** (string). Passwords are bcrypt.

2. **SSO into PreCheck is IMPLEMENTED (this task; full contract in §10).** PreCheck mints
   its own minimal token (`precheck_token`, claims `sub`=*integer* user id + `exp`, its
   own `users` table), so a brain-api JWT (UUID `sub`) is **not** a drop-in `precheck_token`.
   The bridge does **not** reuse the brain JWT — `POST /sso/precheck/token` MINTS a second,
   PreCheck-shaped token (`sub` = the **integer** PreCheck user id, `exp`, HS256, signed
   with the **same** `SECRET_KEY`) for a brain user that (a) belongs to a tenant entitled to
   PreCheck and (b) has a row in the new `precheck_account_links` table mapping their brain
   UUID → PreCheck integer id. PreCheck validates it with its existing auth **unchanged** —
   no PreCheck code change; the only requirement is the shared-secret invariant (§10.5). The
   portal's `/dashboard` is the ported PreCheck app served **same-origin** inside
   `brain-frontend`, so the minted token is written straight to `localStorage["precheck_token"]`
   and the dashboard picks it up — no second login.

3. **Product-access lives in `GET /entitlements`, NOT in the JWT and NOT in
   `/auth/me`.** Per `auth-jwt-multitenant` (no entitlements in the token) and
   `stripe-billing-entitlements` (entitlement resolved server-side at request time).
   `/auth/me` is **identity only**. The frontend calls `/entitlements` after login to
   decide which products to show/link. This matches the existing frontend
   `getEntitlements()` call site.

4. **`POST /demo-requests` is isolated lead capture.** It does NOT create a tenant, does
   NOT touch entitlements, does NOT call Stripe, and triggers NO async work. It is
   separate from PreCheck's pre-existing `/leads/demo-request` funnel.

---

## 1. HTTP conventions

- **Base URL (frontend):** `process.env.NEXT_PUBLIC_MANAGE_API_BASE_URL`. No hardcoded
  domain anywhere. Empty in dev.
- **Content type:** `application/json` for request and response bodies.
- **Auth header:** `Authorization: Bearer <jwt>` on protected endpoints.
- **Error envelope:** FastAPI default — `{"detail": "<message>"}` (string) or, for
  422 validation errors, `{"detail": [ {loc, msg, type}, ... ]}`. The frontend already
  reads `body.detail` (string) and falls back to `res.statusText`.
- **CORS:** brain-api allows the portal origin(s) via `CORS_ALLOW_ORIGINS`
  (comma-separated), `allow_credentials=True`, methods/headers `*`.
- **Timestamps:** ISO-8601 UTC strings (`...Z`) in responses.
- **IDs:** UUID v4 as canonical string form.

---

## 2. Authentication

### 2.1 `POST /auth/token` — login (public)

Exchange email + password for a brain-api access token.

**Request body**
```json
{ "email": "dra.demo@clinica.com.br", "password": "••••••••" }
```
| field | type | rules |
|---|---|---|
| `email` | string | required, valid email, ≤ 320 chars, compared case-insensitively |
| `password` | string | required, 1–72 chars (bcrypt truncates at 72 bytes → reject longer with 422) |

**Response `200`**
```json
{ "access_token": "<jwt>", "token_type": "bearer", "refresh_token": "<opaque>", "expires_in": 1800 }
```
> `access_token`/`token_type` intentionally identical to PreCheck's `TokenResponse`
> and the frontend's existing `LoginResponse` type, so the client stores
> `data.access_token` unchanged. `refresh_token` (opaque, high-entropy — the revocable
> long-lived session leg) and `expires_in` (access lifetime, seconds) are **additive**
> auth-hardening fields; consumers that ignore them keep working (30-min sessions).

**Status codes**
- `200` success
- `401` `{"detail": "Credenciais inválidas"}` — unknown email OR bad password
  (do **not** distinguish the two; same message, constant-time-ish path)
- `422` validation error (malformed email, password > 72 bytes)
- `429` rate limited — per-IP sliding window, `AUTH_RATE_LIMIT_PER_MIN` (default 10/min,
  in-process, fail-open; see §5). Shared budget with `/auth/refresh`.

**JWT claims** (HS256, `SECRET_KEY`, pinned `algorithms=["HS256"]`)
| claim | value |
|---|---|
| `sub` | brain user id, UUID **string** |
| `tenant_id` | tenant UUID **string**, or `null` for a platform `admin` |
| `role` | `"admin"` \| `"tenant_owner"` \| `"tenant_staff"` |
| `iat` | issued-at (UTC) |
| `exp` | `iat + ACCESS_TOKEN_EXPIRE_MINUTES` (default **30**) |

No entitlements, no secrets, no plan flags in the token (skill rule). Verification
also accepts `SECRET_KEY_PREVIOUS` during a rotation window (mint always uses the
current key — `docs/key-rotation.md`). A token carrying a `scope` claim (e.g. the
secretarIA hub token, §12.2) is **rejected** by every user-facing dependency — scoped
service tokens are not user sessions.

---

### 2.1a `POST /auth/refresh` — rotate the session (public, rate-limited)

Body `{"refresh_token": "<opaque>"}` → `200` with a **new** `TokenResponse` pair (same
shape as §2.1). Rotate-on-use: the presented token is revoked and exactly one successor
is issued. Refresh tokens are stored **hashed** (sha256) in `refresh_tokens` (§6.3a) —
a DB read never yields a usable credential.

- `401` unknown / expired / revoked token, or the user no longer exists.
- **Reuse detection:** presenting an already-rotated (revoked) token revokes the
  user's ENTIRE active refresh family and returns `401` — a stolen rotated token
  cannot be replayed, and the legitimate client simply logs in again.
- `429` shared per-IP budget with `/auth/token`.

### 2.1b `POST /auth/logout` — revoke a refresh token (public)

Body `{"refresh_token": "<opaque>"}` → always `204` (no token-existence oracle). Ends
the revocable leg; the short-lived access token simply expires. A plan cancellation or
admin action can likewise revoke rows server-side.

---

### 2.2 `GET /auth/me` — current identity (protected)

Returns the authenticated user + tenant **identity only**. No credentials, no secrets,
no entitlements (`tenant-secrets-encryption` never-leak rule; whitelisted `*Out` schema).

**Auth:** `Authorization: Bearer <jwt>` required.

**Response `200`**
```json
{
  "user": {
    "id": "8f1c…uuid",
    "email": "dra.demo@clinica.com.br",
    "name": "Dra. Demo",
    "role": "tenant_owner"
  },
  "tenant": {
    "id": "2b9a…uuid",
    "clinic_name": "Consultório Dr. Aurélio Lima"
  }
}
```
- `tenant` is `null` when the principal is a platform `admin` (no `tenant_id`).
- **Never** includes `password_hash`, any `*_encrypted` column, or product/plan flags.

**Status codes:** `200`; `401` missing/invalid/expired token.

---

## 3. Entitlements

### 3.1 `GET /entitlements` — resolved entitlement state (protected)

The single source of truth the portal calls to decide which products to show/link and
what plan/limits apply. **Resolved in-process from the local `entitlements` row** keyed
by the JWT's `tenant_id`. **Never** calls Stripe (`stripe-billing-entitlements`).

**Auth:** `Authorization: Bearer <jwt>` required; tenant resolved **server-side** from
the token's `tenant_id` (never from a client-supplied id).

**Response `200`**
```json
{
  "tenant_id": "2b9a…uuid",
  "clinic_name": "Consultório Dr. Aurélio Lima",
  "products": { "precheck": true, "secretaria": true },
  "plan": "complete_clinic_combo",
  "secretaria_tier": "bronze_1",
  "status": "active",
  "addons": {
    "reactivation_pack": true, "verified_identity": true,
    "multi_professional": false, "multi_unit": false, "ehr": false,
    "pix_whatsapp": false, "analytics_bi": false, "human_backup_24_7": false
  },
  "limits": { "professionals": 1, "units": 1, "messages": 400, "reminders": 400, "hsm_proactive": 200 },
  "usage": {}
}
```
| field | type | notes |
|---|---|---|
| `tenant_id` | uuid string | from the token |
| `clinic_name` | string | from `tenants.clinic_name` |
| `products.precheck` | bool | `entitlements.precheck_enabled` |
| `products.secretaria` | bool | `entitlements.secretaria_enabled` |
| `plan` | string | a **catalog plan id** (§3.2); legacy rows may still carry an alias (e.g. `"brain-completo"`) |
| `secretaria_tier` | string \| null | derived from the plan via the catalog: `ferro` \| `bronze_1` \| `bronze_2` \| null |
| `addons` | object | the **full formalized keyset** (§3.2): every add-on id → bool. Normalized through the catalog, so pre-catalog rows still read complete |
| `limits` | object | the **full formalized keyset** (§3.2): every limit key → int ≥ 0 |
| `status` | string | `active` \| `trialing` \| `past_due` \| `canceled` \| `inactive` |
| `usage` | object | usage counters scaffold (metering round), e.g. `{}` or `{"messages": 0}` |

**Resolution rules**
- If no `entitlements` row exists for the tenant, return a **default**: `products` both
  `false`, `plan: "free"`, `status: "inactive"`, `addons`/`limits` full keysets all
  false/zero, `usage: {}`. (Never 404 for a valid tenant — the portal must always render
  a coherent state.)
- `addons`/`limits` are normalized through the catalog on read: catalog defaults for the
  row's plan, with whatever the row materialized layered on top. Product flags are the
  row's own columns (they can diverge from the plan via an explicit admin override).
- `admin` principals (no `tenant_id`): respond `409 {"detail": "No tenant in context"}`
  (the unified portal logs in as a tenant user; admin uses other tooling).

**Status codes:** `200`; `401` missing/invalid token; `409` token has no tenant.

> **Frontend mapping** (`lib/manage-api.ts` `getEntitlements()` → existing
> `Entitlements` type `{ precheck, secretaria, plan, clinicName }`):
> `precheck = products.precheck`, `secretaria = products.secretaria`,
> `plan = plan`, `clinicName = clinic_name`. The `/app` shell consumes those four
> fields unchanged — no dashboard rewrite. (`secretaria_tier` and the populated
> `addons`/`limits` are additive; existing consumers ignore them.)

### 3.2 Catalog — plans, tiers & add-ons (`services/catalog.py`)

The **single source of truth** for everything commercial: plan/add-on ids, which
products each plan enables, the `addons`/`limits` keys it sets, and the (placeholder)
Stripe price ids. Nothing else hardcodes a plan flag or price. Admin PATCH (§11)
materializes entitlement rows FROM the catalog today; the Stripe webhook recompute
(billing round) will reuse the same `compute_entitlement_state` helper.

**Plans** (`plan` column values; `available=false` = reserved, not assignable):

| plan id | products | secretarIA tier | implied add-ons | notes |
|---|---|---|---|---|
| `free` | — | — | — | default / no subscription |
| `precheck` | precheck | — | — | |
| `secretaria_ferro` | secretaria | `ferro` | — | core loop: AI converses + books in Google Calendar; **no reminders** |
| `secretaria_bronze_1` | secretaria | `bronze_1` | — | ferro + automatic 24h/1h HSM reminders |
| `secretaria_bronze_2` | secretaria | `bronze_2` | — | **reserved slot** (`available=false`): no feature set defined yet; not assignable, no Stripe price |
| `complete_clinic_combo` | precheck + secretaria | `bronze_1` | `reactivation_pack`, `verified_identity` | ~15% off the sum (`discount_pct=15` metadata; real price lives in Stripe, billing round) |

Legacy aliases: `"brain-completo"` → `complete_clinic_combo` (old seeded rows keep
their semantics; a PATCH write normalizes to the canonical id). Unknown/stale plan
strings resolve to *no* catalog plan → tier/implied-add-on gates **fail closed**.

**Add-on ids** (the formalized `entitlements.addons` keys — always all present, bool):
`reactivation_pack` (HSM outside the 24h window: 24h/1h reminders + inactive-patient
reactivation), `verified_identity` (Meta Verified for Business), `multi_professional`
(+1 professional per unit), `multi_unit` (+1 unit per unit), `ehr` (iClinic/Doctoralia/
Memed/Conexa), `pix_whatsapp`, `analytics_bi`, `human_backup_24_7`.

**Limit keys** (the formalized `entitlements.limits` keys — always all present, int ≥ 0):
`professionals`, `units`, `messages` (conversations/month), `reminders` (24h/1h HSM
sends/month), `hsm_proactive` (reactivation HSM sends/month). Plan base limits + the
**additive** grants of active add-ons (per unit; Stripe item quantities scale them in
the billing round), computed by `compute_limits`.

**The single gate — `services.entitlements.is_entitled(ent, key)`**: the one helper
every gate reuses (brain-api now; secretarIA's plugin registry consumes the same
semantics over `GET /entitlements`). Pure and in-process — no network, no Stripe.
`key` is an add-on id or a tier. Rules: `status` must be `active`/`trialing` (else
`false`, whatever was bought); an add-on passes if ON in `addons` **or** implied by the
plan (a combo is a plan that implies add-ons); a tier passes if the plan's tier ranks
`>=` the asked tier (tiers are **cumulative**: `ferro` < `bronze_1` < `bronze_2`); an
unknown key raises `ValueError` (programmer error — loud, never silently deny/grant).

---

## 4. Agendar demo (lead capture)

### 4.1 `POST /demo-requests` — public demo request

Backs the "Agendar demo" form (`ContactForm`, Brain + secretarIA variants). Persists a
row to `demo_requests` and returns a confirmation payload. Public, unauthenticated.

**Request body**
```json
{
  "name": "Dr. Aurélio Lima",
  "email": "voce@clinica.com.br",
  "clinic": "Consultório Dr. Aurélio Lima",
  "profile": "clinica_privada",
  "product_interest": "ambos",
  "message": "Quero ver como agenda retornos."
}
```
| field | type | rules |
|---|---|---|
| `name` | string | required, 1–255, trimmed |
| `email` | string | required, valid email, ≤ 320 |
| `clinic` | string \| null | optional, ≤ 255 |
| `profile` | enum \| null | optional; one of `clinica_privada`, `medico_autonomo`, `secretaria_municipal`, `hospital`, `outro` |
| `product_interest` | enum \| null | optional; one of `precheck`, `secretaria`, `ambos` |
| `message` | string \| null | optional, ≤ 2000 |
| `source` | enum \| null | optional client hint; one of `brain`, `secretaria`, `precheck` (defaults to `brain`) |

> The existing `ContactForm` has a **single** radio group whose meaning depends on the
> variant. Frontend mapping (Phase 3): `brain` variant radio → `product_interest`
> (`PreCheck`→`precheck`, `secretarIA`→`secretaria`, `Os dois`→`ambos`); `secretaria`
> variant radio → `profile`. Whichever the form does not collect is sent `null`. Both
> enum fields are therefore **optional** server-side.

**Response `201`**
```json
{
  "id": "c1d2…uuid",
  "status": "new",
  "message": "Recebemos seu pedido! Nossa equipe entra em contato em até 1 dia útil."
}
```

**Status codes**
- `201` created
- `422` validation error (missing name/email, bad enum, oversized field)
- `429` rate limited (basic anti-spam; see §5)

**Persistence:** one row in `demo_requests` (status defaults to `new`). No tenant
creation, no entitlement writes, no Stripe, no async jobs.

---

## 5. Anti-spam / rate limiting (basic)

- `POST /demo-requests`: lightweight per-client-IP limit (e.g. 5 / minute). Keep it
  trivial and in-process (no Redis dependency); on trip return `429`. Optional honeypot
  field (`website`) — if present & non-empty, silently accept-and-drop (`201`, no row).
- `POST /auth/token` + `POST /auth/refresh`: **implemented** — shared per-IP sliding
  window (`AUTH_RATE_LIMIT_PER_MIN`, default 10/min, `core/ratelimit.py`) to blunt
  credential stuffing / token brute force. `0` disables (hermetic tests).

These are best-effort and must never 500 if the limiter backend is unavailable
(fail-open for availability, since no Redis is in play).

---

## 6. Database tables (brain database)

All UUID PKs `default=uuid.uuid4`; `created_at`/`updated_at` are
`DateTime(timezone=True)` with `server_default=func.now()` (and `onupdate=func.now()`
for `updated_at`). Conventions exactly mirror `secretarIA` models.

### 6.1 `tenants` (non-sensitive identity/config)
| column | type | notes |
|---|---|---|
| `id` | UUID | PK |
| `clinic_name` | String(255) | not null |
| `created_at` | DateTime(tz) | server_default now() |
| `updated_at` | DateTime(tz) | server_default now(), onupdate now() |

> Per `tenant-secrets-encryption`, secrets would live in a separate
> `tenant_credentials` table — **not created in this task** (no tenant secrets are
> stored here). The never-leak discipline is still enforced via whitelisted `*Out`
> schemas + structlog redaction.

### 6.2 `users`
| column | type | notes |
|---|---|---|
| `id` | UUID | PK |
| `tenant_id` | UUID FK → `tenants.id` `ON DELETE CASCADE` | **nullable** (null for platform `admin`); indexed |
| `email` | String(320) | **unique**, indexed, not null (store lower-cased) |
| `name` | String(255) | not null |
| `password_hash` | String(255) | not null; **bcrypt** (never serialized/logged) |
| `role` | String(32) | not null; `admin` \| `tenant_owner` \| `tenant_staff` |
| `created_at` | DateTime(tz) | server_default now() |
| `updated_at` | DateTime(tz) | server_default now(), onupdate now() |

### 6.3 `entitlements` (one row per tenant)
Shape from `stripe-billing-entitlements`, extended with the explicit product flags the
task requires.
| column | type | notes |
|---|---|---|
| `tenant_id` | UUID | **PK**, FK → `tenants.id` `ON DELETE CASCADE` |
| `precheck_enabled` | Boolean | not null, server_default false |
| `secretaria_enabled` | Boolean | not null, server_default false |
| `plan` | String(32) | not null, server_default `'free'`; a catalog plan id (§3.2) |
| `status` | String(32) | not null, server_default `'inactive'` |
| `addons` | JSON | not null, server_default `'{}'`; materialized to the full add-on keyset (§3.2) by admin PATCH / future webhook recompute |
| `limits` | JSON | not null, server_default `'{}'`; materialized to the full limit keyset (§3.2) |
| `usage` | JSON | not null, server_default `'{}'` (metering round) |
| `period_start` | DateTime(tz) | nullable |
| `period_end` | DateTime(tz) | nullable |
| `stripe_customer_id` | String(64) | nullable, indexed (scaffold; unused this task) |
| `stripe_subscription_id` | String(64) | nullable (scaffold) |
| `updated_at` | DateTime(tz) | server_default now(), onupdate now() |

### 6.3a `refresh_tokens` (revocable session leg — §2.1a)
| column | type | notes |
|---|---|---|
| `id` | UUID | PK |
| `user_id` | UUID FK → `users.id` `ON DELETE CASCADE` | indexed |
| `token_hash` | String(64) | **unique**, indexed; sha256 hex of the opaque token — the raw value is never stored |
| `expires_at` | DateTime(tz) | not null (`REFRESH_TOKEN_EXPIRE_DAYS`) |
| `revoked_at` | DateTime(tz) | nullable; revoked rows are KEPT for reuse detection |
| `created_at` | DateTime(tz) | server_default now() |

### 6.3b `processed_stripe_events` (webhook idempotency — §13.3)
| column | type | notes |
|---|---|---|
| `id` | String(255) | **PK** — the Stripe `event.id` (natural dedupe key) |
| `event_type` | String(64) | not null |
| `created_at` | DateTime(tz) | server_default now() |

> Inserted in the SAME transaction as the entitlement mutation, so a failed apply
> rolls the marker back and Stripe's redelivery legitimately reprocesses.

### 6.3d `usage_events` (internal usage-event ledger — metering leg, §12.2)
| column | type | notes |
|---|---|---|
| `id` | String(128) | **PK** — the CALLER's own idempotency key (e.g. `"reminder:24h:<appointment_id>"`), not a generated id |
| `tenant_id` | UUID FK → `tenants.id` `ON DELETE CASCADE` | not null, indexed |
| `feature` | String(32) | not null; a catalog `LIMIT_KEYS` id |
| `amount` | Integer | not null |
| `created_at` | DateTime(tz) | server_default now() |

> Inserted in the SAME transaction as the `entitlements.usage[feature]` increment (one
> commit), so the marker and the counter can never drift apart. No Stripe call happens
> on this path — meter-event forwarding is a later billing round (TODO in
> `services/usage.py`).

### 6.4 `demo_requests` (isolated lead capture)
| column | type | notes |
|---|---|---|
| `id` | UUID | PK |
| `name` | String(255) | not null |
| `email` | String(320) | not null, indexed |
| `clinic` | String(255) | nullable |
| `profile` | String(40) | nullable (enum-validated at the schema layer) |
| `product_interest` | String(32) | nullable (enum-validated at the schema layer) |
| `message` | Text | nullable |
| `source` | String(32) | nullable, default `'brain'` |
| `status` | String(32) | not null, server_default `'new'`; `new` \| `contacted` \| `converted` \| `dismissed` |
| `created_at` | DateTime(tz) | server_default now(), indexed |

### 6.5 `precheck_account_links` (SSO identity map — added in migration `0002`)
Maps a brain user (UUID) to their PreCheck user (integer). One row per brain user; it is
the only thing that authorizes minting a PreCheck token for a brain login (§10).
| column | type | notes |
|---|---|---|
| `id` | UUID | PK |
| `brain_user_id` | UUID FK → `users.id` `ON DELETE CASCADE` | **unique** (`uq_precheck_links_brain_user`) — one PreCheck user per brain user |
| `precheck_user_id` | BigInteger | not null, **unique** (`uq_precheck_links_precheck_user`); logical ref to `precheck.users.id` in PreCheck's **separate** DB — **no FK** by design |
| `tenant_id` | UUID FK → `tenants.id` `ON DELETE CASCADE` | not null, indexed; asserted to match the acting principal's tenant before minting |
| `created_at` | DateTime(tz) | server_default now() |

### 6.3c `privacy_requests` (LGPD audit trail — §14)
| column | type | notes |
|---|---|---|
| `id` | UUID | PK |
| `kind` | String(16) | not null; `erasure` \| `export` |
| `subject_type` | String(16) | not null; `patient` \| `user` |
| `subject_hash` | String(64) | not null, indexed; sha256 hex of the **normalized** subject key — NEVER the raw email/wa_id |
| `requested_by` | UUID FK → `users.id` `ON DELETE SET NULL` | nullable; the acting admin (the audit row outlives the admin account) |
| `status` | String(16) | not null; `completed` \| `partial` |
| `result` | JSON | not null; COUNTS + per-service status ONLY — never personal data |
| `created_at` | DateTime(tz) | server_default now() |

Migration **`0001`** creates `tenants`/`users`/`entitlements`/`demo_requests`; migration
**`0002`** adds `precheck_account_links`; migration **`0003`** adds `refresh_tokens` +
`processed_stripe_events`; migration **`0004`** adds `privacy_requests`; migration
**`0005`** adds `usage_events`.

---

## 7. Configuration / env vars (brain-api `.env`)

| var | default | purpose |
|---|---|---|
| `APP_ENV` | `dev` | env name (`dev`/`staging`/`production`) |
| `LOG_LEVEL` | `INFO` | structlog level |
| `DATABASE_URL` | `postgresql+asyncpg://…/brain` | the **brain** database |
| `SECRET_KEY` | — | JWT HS256 signing key. **MUST be byte-identical to the PreCheck backend's `SECRET_KEY`** — the minted SSO token (§10) is only valid if both services share it |
| `SECRET_KEY_PREVIOUS` | `""` | rotation window ONLY: old key accepted for verification while issued tokens age out; mint always uses `SECRET_KEY` (`docs/key-rotation.md`) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | brain access-token TTL (short; refresh is the long leg) |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `14` | refresh-token TTL (§2.1a; hashed at rest, rotate-on-use) |
| `AUTH_RATE_LIMIT_PER_MIN` | `10` | per-IP `/auth/token` + `/auth/refresh` budget (§5); `0` disables |
| `HUB_TOKEN_EXPIRE_MINUTES` | `60` | TTL of the minted secretarIA hub token (§12.2) |
| `PRECHECK_TOKEN_EXPIRE_MINUTES` | `60` | TTL of the minted PreCheck SSO token (§10); matches PreCheck's own session length |
| `CORS_ALLOW_ORIGINS` | `http://localhost:3000` | comma-separated portal origins |
| `DEMO_RATE_LIMIT_PER_MIN` | `5` | basic demo-request anti-spam |
| `ADMIN_EMAIL` | `""` | platform admin bootstrap (`scripts/seed_admin.py`); env-only, never in code |
| `ADMIN_PASSWORD` | `""` | platform admin bootstrap; bcrypt-hashed on insert, never logged |
| `IMPERSONATION_DEMO_EMAIL` | `dra.demo@clinica.com.br` | tenant (clinic) owner the admin "Modo médico" switch enters (§11.4). Must be a `tenant_owner`/`tenant_staff` carrying a `tenant_id`, else `POST /admin/impersonate/token` is `404`. Defaults to the seeded dev clinic; in production point at a real sandbox clinic owner |
| `PRECHECK_BASE_URL` | `""` | PreCheck backend base URL for the BFF read proxy (§11.1). Empty → list proxies return an empty page locally |
| `PRECHECK_TIMEOUT_SECONDS` | `10` | timeout for the precheck proxy httpx client |
| `PRECHECK_INTERNAL_TOKEN` | `""` | PreCheck's internal/n8n token (`X-Internal-Token`) — a service credential, distinct from the forwarded-brain-JWT proxy above. Used for the LGPD privacy orchestration (§14; empty → the precheck leg of an erasure/export reports `skipped_unconfigured`, the rest still runs) **and** the secretarIA→PreCheck patient handoff (§12.3; empty **or** `PRECHECK_BASE_URL` empty → `503 precheck_handoff_not_configured`, fails LOUD — no degrade) |
| `SECRETARIA_BASE_URL` | `""` | secretaria base URL. Used by the doctor appointments/patients data path (§12.1) **and** the admin connection (§11.2). Empty → §12.1 returns an empty page; §11.2 routes return `503` |
| `SECRETARIA_API_KEY` | `""` | **the brain↔secretaria PAIR key** (one secret, both directions, §12.1): sent as `X-Internal-Api-Key` for brain-api → secretaria `/internal/*`, AND verified on brain-api's own inbound `/internal/*` (§12.2 — secretarIA introspecting hub tokens / reading entitlements). **MUST equal secretaria's own `INTERNAL_API_KEY`** byte-for-byte; a **mismatch** ⇒ `401` ⇒ caller surfaces `502`. **Unset on either side** ⇒ outbound data path degrades to an empty page; inbound surface fails closed `403`. Random machine secret; never logged |
| `SECRETARIA_API_KEY_PREVIOUS` | `""` | rotation window ONLY: old pair key accepted on the inbound verifier (`docs/key-rotation.md`) |
| `SECRETARIA_ADMIN_TOKEN` | `""` | secretaria admin connection (§11.2): sent as `X-Admin-Token`. **MUST equal secretaria's own `ADMIN_TOKEN`** or secretaria returns `403`. A DESTRUCTIVE-endpoint secret — never logged. Empty → §11.2 routes `503` |
| `SECRETARIA_TIMEOUT_SECONDS` | `10` | timeout for the secretaria admin httpx client |
| `STRIPE_SECRET_KEY` | `""` | Stripe secret API key (§13). Empty ⇒ billing endpoints `503`; entitlement reads never touch Stripe regardless |
| `STRIPE_WEBHOOK_SECRET` | `""` | webhook signing secret (§13.3). Empty **fails CLOSED**: every delivery rejected `400` |
| `STRIPE_PRICE_MAP` | `{}` | JSON: catalog id (§3.2) → Stripe price id, per environment. Unknown ids rejected at parse time |
| `STRIPE_CHECKOUT_SUCCESS_URL` / `STRIPE_CHECKOUT_CANCEL_URL` / `STRIPE_PORTAL_RETURN_URL` | localhost portal | browser return URLs for Checkout / Billing Portal |
| `STRIPE_API_BASE` / `STRIPE_TIMEOUT_SECONDS` | `https://api.stripe.com` / `15` | Stripe HTTP client (async httpx; the SDK is used only for webhook signature verification) |

`get_settings()` is `@lru_cache`d. `cors_origins` is a parsed-list property.

---

## 8. Frontend call sites (Phase 3)

Typed client functions, env-based base URL, no hardcoded domain:

| client fn (in `lib/manage-api.ts`) | endpoint | used by |
|---|---|---|
| `login(email, password)` | `POST /auth/token` | the existing login screen (design kept) |
| `getMe()` | `GET /auth/me` | optional identity hydrate |
| `getEntitlements(session)` | `GET /entitlements` (Bearer) | `/app` dashboard shell |
| `submitDemoRequest(payload)` | `POST /demo-requests` | `ContactForm` (Brain + secretarIA) |
| `getPrecheckSsoToken(session)` | `POST /sso/precheck/token` (Bearer) | `/app` "Abrir painel completo" → store `precheck_token` → `/dashboard` |

- Login flow: existing design (`AuthShell` + `PasswordField`) → `login()` → store JWT in
  the brain session (`brain.session`, sessionStorage — the key `/app` already reads) →
  route to `/app` → `getEntitlements()` → conditionally show/link PreCheck + SecretarIA.
- The **invented standalone login screen does not exist** in the current tree (verified)
  — nothing to remove; the existing `/login` design is the one we keep/wire.
- `ContactForm.handleSubmit` calls `submitDemoRequest()` and shows the existing success
  state on `201`.
- **PreCheck SSO handoff:** `/app`'s "Abrir painel completo" calls `getPrecheckSsoToken()`,
  writes the returned token to `localStorage["precheck_token"]` (the ported `lib/auth.ts
  setToken`, same origin as `/dashboard`), then routes to `/dashboard`. `403
  precheck_not_entitled` and `409 precheck_account_not_linked` render inline messages (via
  `ManageApiError.status`), never a crash.

---

## 9. Skill compliance map (which skill governs each backend module)

| module | skill |
|---|---|
| `core/security.py` (JWT mint/verify, bcrypt), `api/deps.py` (Principal, role/tenant scope) | `auth-jwt-multitenant` |
| `models/entitlement.py`, `services/entitlements.py` (`resolve_entitlement`, `is_entitled`), `GET /entitlements` | `stripe-billing-entitlements` |
| `services/catalog.py` (plans/tiers/add-ons/limits source of truth, §3.2), admin PATCH materialization in `services/admin.py` | `stripe-billing-entitlements` (catalog feeds the local row; reads never touch Stripe; prices only as placeholder ids until the billing round) |
| `schemas/*Out` whitelists (no `*_encrypted`/`password_hash`), `core/logging.py` `redact_secrets` | `tenant-secrets-encryption` |
| `POST /demo-requests` (sync, isolated) | n/a — `whatsapp-webhook-arq` explicitly skipped (no async work) |
| `core/security.py create_precheck_token`, `services/sso.py`, `api/sso.py` | `auth-jwt-multitenant` (pinned HS256, minimal claims, short TTL, shared secret) |
| `services/secretaria_internal.py` (`X-Internal-Api-Key` data calls), secretaria `api/internal.py` (`require_internal_api_key`) | `auth-jwt-multitenant` (service-to-service shared key, fail-closed, constant-time compare, key never logged) |
| `services/sso.py` entitlement gate (reuses `resolve_entitlement`) | `stripe-billing-entitlements` (in-process read, never Stripe) |
| `models/precheck_link.py` (identity map; never serialized/logged) | `tenant-secrets-encryption` (never-leak posture) |
| `services/billing.py`, `api/billing.py` (checkout/portal + webhook recompute, §13), `models/processed_stripe_event.py` | `stripe-billing-entitlements` (webhook = sole billing writer; signature verified; idempotent per event id; reads never call Stripe) |
| `models/refresh_token.py`, `services/auth.py` refresh flow, `POST /auth/refresh|logout`, `core/ratelimit.py` | `auth-jwt-multitenant` (short access TTL + revocable hashed refresh, rotate-on-use, reuse detection, rate-limited login) |
| `models/privacy_request.py`, `services/privacy.py`, `api/privacy.py` (§14) | `auth-jwt-multitenant` (router-level admin gate, service-to-service shared credentials into secretaria/precheck, fail-closed on missing config) + `tenant-secrets-encryption` (never-leak: `subject_hash` not the raw identifier, audit `result` is counts-only, no `password_hash` in an export) |
| `core/security.py` hub token (`create_hub_token`/`decode_hub_token`), `api/internal.py` (inbound pair-key gate + introspection), `POST /doctor/secretaria/hub-token` | `auth-jwt-multitenant` (purpose-scoped token, live server-side entitlement check, no user JWT into secretarIA) |

---

## 10. Cross-product SSO — PreCheck handoff (implemented)

The bridge that lets a PreCheck-entitled brain user open the PreCheck dashboard from the
portal **without a second login**. brain-api does **not** proxy PreCheck and does **not**
reuse the brain JWT; it mints a separate, PreCheck-shaped token and lets PreCheck validate
it with its existing, **unchanged** auth.

### 10.1 `POST /sso/precheck/token` — mint a PreCheck session (protected)

**Auth:** `Authorization: Bearer <brain jwt>` required; tenant resolved server-side
(`require_tenant` — a platform `admin` with no tenant gets `409 "No tenant in context"`).

**Flow (`services/sso.py`):**
1. Resolve entitlements in-process (the same `resolve_entitlement` as §3 — **no Stripe**).
   If `products.precheck` is false → **`403 {"detail": "precheck_not_entitled"}`**.
2. Look up `precheck_account_links` by `brain_user_id` (the JWT `sub`). No row →
   **`409 {"detail": "precheck_account_not_linked"}`** (a typed signal, not a crash — the
   portal shows "ask your admin to connect your PreCheck account"). Defense-in-depth: if
   the link's `tenant_id` ≠ the principal's tenant, also `409`.
3. Mint with `create_precheck_token(link.precheck_user_id)` and return it.

**Response `200`**
```json
{ "token": "<precheck-compatible jwt>", "token_type": "bearer", "expires_in": 3600 }
```
| field | type | notes |
|---|---|---|
| `token` | string | PreCheck-shaped JWT (see §10.2) |
| `token_type` | string | `"bearer"` |
| `expires_in` | int | seconds; `PRECHECK_TOKEN_EXPIRE_MINUTES × 60` |

**Status codes:** `200`; `401` missing/invalid brain token; `403` not entitled to PreCheck;
`409` no tenant in context **or** account not linked.

### 10.2 The minted token (how it conforms to PreCheck)

PreCheck validates with `jose.jwt.decode(token, SECRET_KEY, algorithms=["HS256"])` and then
`User.id == int(payload["sub"])` (PreCheck `app/core/security.py` + `app/core/deps.py`). It
reads **only** `sub` and `exp`. The minted token therefore is:

| claim | value |
|---|---|
| `sub` | the **integer** `precheck_user_id`, as a **string** (PreCheck casts to `int`) |
| `iat` | issued-at (UTC) — hygiene; PreCheck ignores it |
| `exp` | `iat + PRECHECK_TOKEN_EXPIRE_MINUTES` (default 60) |

Algorithm **HS256**, signed with **`SECRET_KEY`**. No brain identity, tenant, role, or
secret rides along — only what PreCheck's verifier needs (`auth-jwt-multitenant`).

### 10.3 Lifetime

The minted token **becomes** the PreCheck session (the ported dashboard stores it as
`precheck_token` and uses it for every PreCheck-backend call), so its TTL is the PreCheck
session length — matched to PreCheck's own default (60 min) via
`PRECHECK_TOKEN_EXPIRE_MINUTES`. The handoff is same-origin (written directly to localStorage,
never placed in a URL/Referer/log), so there is no URL-leak surface demanding a shorter
bootstrap token.

### 10.4 Frontend handoff (same-origin)

`/dashboard` is the **ported PreCheck app inside `brain-frontend`** (route group `(SignIn)`),
served from the **same origin** as `/app`. So `/app` writes the minted token to
`localStorage["precheck_token"]` (the ported `lib/auth.ts setToken`) and navigates to
`/dashboard`; the dashboard's existing guard (`isAuthed()` → `precheck_token`) passes and its
`lib/api.ts` sends `Authorization: Bearer <token>` to the **real PreCheck backend**
(`NEXT_PUBLIC_API_URL`), which validates as in §10.2. No token ever appears in a URL.

### 10.5 Deployment invariant (REQUIRED) & onboarding

- **Shared secret:** brain-api `SECRET_KEY` **must equal** the PreCheck backend `SECRET_KEY`
  (both read the env var `SECRET_KEY`; PreCheck via `app/core/config.py` `secret_key`,
  brain-api via `config.py` `SECRET_KEY`). If they differ, PreCheck rejects the minted token
  with 401. This is the **only** coupling; no PreCheck code changed. Rotating this key
  without SSO downtime is a two-deploy procedure (`SECRET_KEY_PREVIOUS` verify window on
  brain-api, then flip PreCheck) — `docs/key-rotation.md` §1.
- **Creating a link (onboarding):** `uv run python scripts/link_precheck_account.py
  --brain-email <email> --precheck-user-id <int>` (idempotent; guards the reverse-unique).
  The PreCheck integer id comes from PreCheck's own users table (separate DB). For a local
  end-to-end, `make seed` also creates the demo link when `DEMO_PRECHECK_USER_ID` is set.
- **No PreCheck change required.** PreCheck's backend already trusts any `SECRET_KEY`-signed
  HS256 token whose `sub` resolves to a real `users.id`; its frontend isn't in the path (the
  portal uses the same-origin ported copy). Had the portal instead linked to a *separate*
  PreCheck origin, a thin token-intake route on PreCheck would have been required — it is not,
  here.

---

## 11. Platform-admin API (RBAC round) — role `admin`

Every `/admin/*` route is gated by `require_role("admin")` at the **router** level: a
non-admin brain JWT gets `403 {"detail": "Insufficient role"}`, no token `401`, before any
handler runs. Admins have `tenant_id = null` and act across all tenants. Responses use
whitelisted `*Out` schemas (never `password_hash` / `*_encrypted`). List endpoints share a
**`Page`** envelope `{ "items": [...], "total": N, "skip": s, "limit": l }` with `skip >= 0`,
`1 <= limit <= 100` (default 50), newest first.

| method | path | notes |
|---|---|---|
| `GET` | `/admin/tenants` | `Page` of `{id, clinic_name, created_at, plan, status, precheck_enabled, secretaria_enabled, users_count}` |
| `GET` | `/admin/tenants/{tenant_id}` | detail `{id, clinic_name, created_at, updated_at, users_count, entitlements{…}}`; `404` if unknown. **No credentials fields** |
| `GET` | `/admin/tenants/{tenant_id}/entitlements` | entitlement record (coherent defaults if no row); `404` unknown tenant |
| `PATCH` | `/admin/tenants/{tenant_id}/entitlements` | partial `{precheck_enabled?, secretaria_enabled?, plan?, status?, addons?, limits?}`; **upserts** the row; `404` unknown tenant; **catalog-validated** (§3.2): `plan` must be an assignable catalog plan (legacy aliases normalize to the canonical id; reserved slots rejected), `addons` keys must be known add-on ids, `limits` keys known limit keys with values ≥ 0 — else `422`. **Materialization order:** `plan` first rewrites products+`addons`+`limits` from the catalog (`compute_entitlement_state`); explicit fields in the same patch override it; a patched `addons` normalizes to the full keyset and recomputes `limits`; an explicit `limits` merges on top as a manual override. How a product is manually switched on pre-Stripe |
| `GET` | `/admin/users` | `Page` of `{id, tenant_id, clinic_name|null, email, name, role, created_at}`. **Never** `password_hash` |
| `POST` | `/admin/users` | `201` create in any tenant/role. Body `{email, name, password, role, tenant_id?}`. **Password policy: 8–72 chars, at least one letter and one digit** (bcrypt's 72-byte ceiling; `422` otherwise). `admin` ⇒ `tenant_id` must be null; tenant roles ⇒ `tenant_id` required+existing. `409` dup email, `404` unknown tenant, `422` bad combo / policy violation |
| `GET` | `/admin/demo_requests` | `Page` of brain's own demo leads, newest first |
| `PATCH` | `/admin/demo_requests/{id}` | set `status ∈ {contacted, converted, dismissed}`; `404` unknown, `422` other value. (Portal actions "Marcar como contatado" / "Converter em tenant" / "Descartar") |
| `GET` | `/admin/inbound` | **proxy** → PreCheck `GET /api/v1/admin/inbound` (§11.1); returns PreCheck's payload verbatim |
| `GET` | `/admin/secretaria/tenants` | **proxy** → secretaria `GET /admin/tenants` (§11.2); clinics + calendar health, verbatim |
| `POST` | `/admin/secretaria/reset` | **proxy** → secretaria `POST /admin/reset` (§11.2). **DESTRUCTIVE.** Body `{confirm: true, include_tenants?: false}`; `400` if `confirm` not true |
| `POST` | `/admin/impersonate/token` | mint a tenant-scoped **doctor** token for the admin "Modo médico" switch (§11.4). No body; targets `IMPERSONATION_DEMO_EMAIL`. `404 impersonation_target_unavailable` if that clinic is not seeded/configured |

### 11.1 brain-api → PreCheck read proxy (supersedes §0's "not called")

The RBAC round adds read proxies where brain-api forwards the **caller's brain JWT**
verbatim to PreCheck, which validates it itself (`precheck app/core/brain_auth.py`) and
role-gates/scopes the result. There is **no separate internal key**: the same token that
authorized the brain-api route authorizes the upstream call. `PRECHECK_BASE_URL` selects the
upstream; **unset** ⇒ list proxies return an empty page `{items:[], total:0, …, "stub":true}`
(keeps the portal rendering locally) and detail ⇒ `503 precheck_not_configured`. An upstream
`4xx` (e.g. PreCheck's own `403` for a non-admin) is surfaced verbatim; `5xx`/network ⇒ `502`.
The forwarded `Authorization` header is never logged. Proxy routes: `GET /admin/inbound`,
`GET /doctor/anamneses[/{id}]`.

### 11.2 brain-api → secretaria admin connection (service-to-service)

secretaria has **no user/role system** — its only privileged surface is `/admin/*`, guarded
by a shared secret (`X-Admin-Token` vs secretaria's `ADMIN_TOKEN`). So a brain `admin`
**cannot SSO into secretaria** the way a doctor SSOs into PreCheck. Instead brain-api calls
secretaria's admin routes **on the admin's behalf**, presenting `SECRETARIA_ADMIN_TOKEN`
(≠ the PreCheck pattern: nothing from the caller is forwarded — secretaria has no notion of
the caller). The brain `admin` role is enforced by the router gate; the service credential
lives in `services/secretaria_client.py` and is never logged or echoed. `SECRETARIA_BASE_URL`
**or** `SECRETARIA_ADMIN_TOKEN` unset ⇒ `503` (`secretaria_not_configured` /
`secretaria_admin_not_configured`) **before any network call**; secretaria's own `4xx` (e.g.
`403` bad token) surfaces verbatim; `5xx`/network ⇒ `502`. `POST /admin/secretaria/reset` is
**destructive** and double-guarded: the router `admin` gate **and** an explicit `confirm:true`
body (a missing/false confirm is `400`, before any upstream call); invocations log a WARNING
with the actor id.

### 11.3 Cross-mesh super-admin provisioning (`scripts/provision_superadmin.ps1`)

There is **no DB cascade** (three separate databases). One super-admin is provisioned per
service via that service's own mechanism, orchestrated by the mesh-root script:
**(1) brain-api** `scripts/create_admin.py` → platform admin (`role=admin`, `tenant_id=NULL`);
**(2) PreCheck** its existing `python -m app.scripts.create_admin --superadmin` → native
superadmin (`role=admin`, `clinic_id=NULL`), **no PreCheck code change**;
**(3) secretaria** nothing to create — the script only **verifies** the §11.2 admin link.
The brain admin already reaches PreCheck's brain-portal admin routes via the shared
`SECRET_KEY`; the PreCheck superadmin row is what unlocks PreCheck's **native** console
(`/admin/clinics`, `/admin/users`, rotate-key), which a brain UUID-`sub` token cannot.
**Admin SSO is intentionally not wired** — `/sso/precheck/token` requires a tenant and admins
have none; brain portal and PreCheck remain separate logins. The **one** deliberate exception
is the admin-only "Modo médico" dev switch (§11.4), which does not SSO the admin *as an admin*
but instead lets them **act as a chosen clinic's doctor** via a minted tenant-scoped token.

### 11.4 Admin "Modo médico" impersonation (`POST /admin/impersonate/token`)

A platform admin has no tenant, so by default it cannot use the doctor portal or open
PreCheck/secretarIA (every `/doctor/*` route + `/sso/precheck/token` reject a tenant-less
token — §12, §10). This endpoint is an **admin-only convenience for developing the website +
API**: it lets an admin enter the doctor portal **as a real clinic user**, with live data, in
one click — without a second login.

**Auth:** `Authorization: Bearer <admin jwt>`; the router-level `require_role("admin")` gate is
the sole authorization (a non-admin gets `403`). **No request body.**

**Flow (`services/admin.issue_impersonation_token`):**
1. Resolve `IMPERSONATION_DEMO_EMAIL` (the configured demo/sandbox clinic owner) to a user.
   If absent, tenant-less, or not a `tenant_owner`/`tenant_staff` → **`404
   {"detail": "impersonation_target_unavailable"}`** (an admin must never become a "doctor
   with no tenant", which would violate `require_doctor`'s invariant).
2. Mint a **normal** access token via `create_access_token(sub=<doctor user>, tenant_id, role)`
   — byte-identical in shape to that user's own `/auth/token` login (§2.1). No extra claim, no
   secret. The admin's own token is untouched and not embedded.

**Response `200`**
```json
{
  "access_token": "<doctor jwt>",
  "token_type": "bearer",
  "tenant_id": "2b9a…uuid",
  "clinic_name": "Consultório Dr. Aurélio Lima",
  "email": "dra.demo@clinica.com.br",
  "role": "tenant_owner",
  "expires_in": 3600
}
```
The non-`access_token` fields are non-secret display data for the portal's "you are in doctor
mode" banner. **Status codes:** `200`; `401` missing/invalid admin token; `403` not an admin;
`404` target clinic not seeded/configured.

**Audit:** each mint logs `admin_impersonation_issued` at **WARNING** with the acting admin's
`user_id` + the target user/tenant/role. The minted token is **never** logged.

**Frontend handoff (`brain-frontend`, same-origin sessionStorage):** the admin header's "Modo
médico" button calls this, **stashes** the admin session, swaps the minted doctor session into
`brain.session`, records a `brain.impersonation` marker, and routes to `/doctor/dashboard`. The
doctor portal shows a banner whose "Voltar ao admin" **restores** the stashed admin session.
Because the minted token is an ordinary doctor token, the existing doctor guards + `/doctor/*`
calls + the PreCheck SSO all work unchanged. (Logging out clears the marker + stash.)

---

## 12. Doctor (tenant) API (RBAC round) — roles `tenant_owner` / `tenant_staff`

Every `/doctor/*` route is gated by `require_doctor` at the router level: a valid brain JWT
whose `role ∈ {tenant_owner, tenant_staff}` **and** that carries a `tenant_id`. A platform
`admin` token gets `403` (wrong portal — admins use `/admin/*`). The acting tenant is ALWAYS
`principal.tenant_id` from the validated token; **`tenant_id` is never accepted as a query or
body param**, so a doctor cannot read another tenant's data by forging an id.

| method | path | notes |
|---|---|---|
| `GET` | `/doctor/me` | `{user{id,email,name,role}, tenant{id,clinic_name}, entitlements{…}}`. Identity + products, no secrets |
| `GET` | `/doctor/appointments` | **proxy** → secretaria `GET /internal/tenants/{tenant_id}/appointments` (§12.1), `X-Internal-Api-Key`, scoped to `principal.tenant_id`. `{"data": [...]}`; query `skip>=0`, `1<=limit<=100`. Unconfigured mesh → `{"data": [], "stub": true}` |
| `GET` | `/doctor/patients` | **proxy** → secretaria `GET /internal/tenants/{tenant_id}/patients` (§12.1) (same auth/scope/fallback as appointments) |
| `GET` | `/doctor/anamneses` | **proxy** → PreCheck `GET /api/v1/doctor/anamneses` (§11.1); tenant-scoped by the forwarded token |
| `GET` | `/doctor/anamneses/{id}` | **proxy** → PreCheck `GET /api/v1/doctor/anamneses/{id}`; PreCheck enforces the record belongs to the token's tenant/clinic |
| `POST` | `/doctor/secretaria/hub-token` | mint the tenant-scoped secretarIA **hub token** (§12.2). Entitlement-gated: `403 secretaria_not_entitled` unless status active/trialing AND secretaria enabled. `200 {hub_token, token_type, expires_in}` |

### 12.1 brain-api → secretaria internal data connection (service-to-service)

The doctor portal's appointments + patients come from `secretaria`, which is **internal-only**
(no browser, no human, no JWT ever reaches it — `auth-jwt-multitenant`). secretaria exposes an
`/internal/*` surface guarded by a shared **`X-Internal-Api-Key`** secret; brain-api calls it
with `SECRETARIA_API_KEY` (`services/secretaria_internal.py`). This is a **third, distinct**
service-to-service shape:

- **vs PreCheck proxy (§11.1):** PreCheck forwards the caller's *brain JWT*; here nothing from
  the caller is forwarded — secretaria has no notion of the user. The acting tenant is carried
  in the **URL path** and filled by brain-api from `principal.tenant_id` (never a client param),
  so a doctor cannot read another tenant's data by forging an id.
- **vs secretaria admin (§11.2):** that uses `X-Admin-Token` / `SECRETARIA_ADMIN_TOKEN` on
  `/admin/*` (a different secret + surface). `SECRETARIA_API_KEY` ≠ `SECRETARIA_ADMIN_TOKEN`.

**Key pairing (deployment invariant):** brain-api `SECRETARIA_API_KEY` **must equal** secretaria's
own `INTERNAL_API_KEY`, byte-for-byte (set both in Easypanel; generate with
`python -c "import secrets; print(secrets.token_hex(32))"`). A random machine secret — it comes
from no external provider. On **either** side empty / mismatched: secretaria's `require_internal_api_key`
rejects with **401** (missing/wrong key) or **403** (server key unconfigured); the key is never logged.
**This same PAIR key now guards both directions** — the billing/auth round added an inbound
`/internal/*` surface on brain-api that secretarIA calls with its `INTERNAL_API_KEY` (§12.2); one
secret, two directions, rotated together via the `*_PREVIOUS` verify window (`docs/key-rotation.md` §2).

**brain-api behaviour (`services/secretaria_internal.py`):** an **unconfigured** mesh degrades, a
**misconfigured** one errors. `SECRETARIA_BASE_URL` **or** `SECRETARIA_API_KEY` unset *here* ⇒ the
list calls fail closed to an empty page `{"data": [], "stub": true}` (the portal still renders) —
**no** upstream call, **no** `500`. If **secretaria's own** key is unset it answers `403`, which
brain-api **also** degrades to that empty page (an unset key on **either** side ⇒ empty page, per
acceptance criterion #1). A genuinely failing upstream — network, secretaria `5xx`, **or** a key
**mismatch** (both sides set but different ⇒ secretaria `401`) — collapses to **`502`** with a
generic detail; secretaria's status/body is never surfaced and a key problem is never mis-reported
as the doctor's own `401`. Routes: `GET /doctor/appointments`, `GET /doctor/patients`.

**secretaria endpoints** (`api/internal.py`, every route `Depends(require_internal_api_key)`):
`GET /internal/tenants/{tenant_id}/appointments` and `.../patients`, each `{"data": [...]}` with
lean per-row schemas (`schemas/internal.py`) that deliberately omit internal Google ids; query
`limit` (`1..200`, default 50) + `offset` (`>=0`).

### 12.2 secretarIA doctor-hub auth — hub token + introspection (billing/auth round)

secretarIA's doctor hub (`api/hub/*`: config, calendar, OAuth) authenticates a bearer
token via `core/subscription.py::verify_subscription_token`. That seam is now REAL
(the MVP fake token is gone):

1. **Mint (brain-api):** `POST /doctor/secretaria/hub-token` (§12 table) mints a
   **purpose-scoped** JWT — claims `{sub: <tenant_id>, scope: "secretaria_hub",
   act: <acting user id>, iat, exp}` (`HUB_TOKEN_EXPIRE_MINUTES`, default 60), signed
   with `SECRET_KEY`. It is **NOT a user JWT**: it has no `role`/`tenant_id` claims, and
   brain-api's own `get_current_principal` rejects any token carrying a `scope` claim.
   No user JWT ever reaches secretarIA.
2. **Present (portal → secretarIA hub):** the portal sends it as
   `Authorization: Bearer <hub_token>` to secretarIA's hub endpoints.
3. **Introspect (secretarIA → brain-api):** secretarIA never validates it locally. It
   calls `POST /internal/secretaria/hub-token/verify` with `X-Internal-Api-Key` (the
   §12.1 PAIR key) and body `{"token": ...}`. brain-api answers
   `{"active": bool, "tenant_id": uuid|null}` where `active` requires ALL of: valid
   signature/expiry/scope, entitlement status `active|trialing`, `secretaria_enabled`.
   The entitlement is re-read LIVE on every introspection — a cancellation locks the
   hub within secretarIA's short positive-cache TTL (`SUBSCRIPTION_CACHE_TTL_SECONDS`,
   default 60s). secretarIA FAILS CLOSED on any error/unconfigured mesh.

**brain-api inbound `/internal/*` surface** (`api/internal.py`; every route gated by
the PAIR key, fail-closed 403 unconfigured / 401 mismatch, constant-time, never logged;
accepts `SECRETARIA_API_KEY_PREVIOUS` during rotation):

| method | path | notes |
|---|---|---|
| `POST` | `/internal/secretaria/hub-token/verify` | introspection above. Always `200` for an authenticated service caller — refusal is `active:false`, not an HTTP error |
| `GET` | `/internal/tenants/{tenant_id}/entitlements` | entitlement summary `{tenant_id, status, active, secretaria_enabled, plan, secretaria_tier, addons, limits}` — the gate data secretarIA's plugin registry consumes (same `is_entitled` semantics, §3.2) |
| `POST` | `/internal/usage-events` | metering leg only (`stripe-billing-entitlements`; NO Stripe call — meter forwarding is a later billing round). Body `{tenant_id, feature, amount, event_id}` — `feature` must be a catalog `LIMIT_KEYS` id (422 otherwise), `amount` `1..10000`, `event_id` is the CALLER's own idempotency key (e.g. `"reminder:24h:<appointment_id>"`). Inserts a `usage_events` row (§6.3d) AND increments `entitlements.usage[feature]` in ONE transaction (upserts the entitlement row if missing). Always `200 {recorded: bool}` — `false` means `event_id` was already applied (replay), no double-count, never an HTTP error |
| `POST` | `/internal/precheck-handoff` | secretarIA → brain-api → PreCheck patient handoff (§12.3). Body `{tenant_id, phone_number}` — `phone_number` digits only, `8..15` chars (422 otherwise). Entitlement-gated: `403 precheck_not_entitled` unless status active/trialing AND `precheck_enabled`. Forwards to PreCheck; full status matrix in §12.3. No DB write |

### 12.3 secretarIA → PreCheck patient handoff (`POST /internal/precheck-handoff`)

**Purpose:** when a patient messages a clinic's WhatsApp number and secretarIA
recognizes them (or the flow otherwise needs one), secretarIA calls brain-api to
**pre-seed** a PreCheck session for that patient — a session that already exists the
moment PreCheck's own webhook sees the first message, so PreCheck's usual "figure out
who this is from keyword matching on the first message" bootstrap is skipped entirely.
brain-api does **not** create or store anything for this: it is a single orchestration
hop, entitlement-gated, with no new local state (one leg of three — the sibling legs
live in secretarIA and PreCheck, built independently against this same frozen
contract).

**Hub-and-spoke rationale:** secretarIA **never** calls PreCheck directly. brain-api is
the identity/entitlement **authority** (`auth-jwt-multitenant`,
`stripe-billing-entitlements`) for every tenant in the mesh — secretarIA has no
notion of whether a tenant is entitled to PreCheck, and PreCheck is deliberately never
asked to re-check it (a tenant that loses its PreCheck entitlement must stop getting
seeded sessions the moment brain-api's row says so, with no separate place for that
rule to drift out of sync). brain-api sits at the hub of both spokes it already
maintains — `SECRETARIA_API_KEY` inbound (§12.1/§12.2) and `PRECHECK_INTERNAL_TOKEN`
outbound (§14) — so this handoff reuses both without introducing a third pairwise
service credential.

**Inbound (secretarIA → brain-api):** joins the existing `/internal/*` router
(`api/internal.py`), gated by the SAME pair key as every other route on it
(`X-Internal-Api-Key` = `SECRETARIA_API_KEY`, §12.1/§12.2; fail-closed `403`
unconfigured / `401` mismatch). Body `{"tenant_id": "<uuid>", "phone_number":
"<digits>"}`; `phone_number` must match `^\d{8,15}$` and `tenant_id` must parse as a
UUID, else `422`.

**Entitlement gate (brain-api is the authority — checked BEFORE any upstream call):**
the tenant's local `entitlements` row (`services/entitlements.py::resolve_entitlement`,
same resolution as every other gate in this file) must have `status` in
`active`/`trialing` **AND** `precheck_enabled = true`. Anything else — no row, wrong
status, or the product flag off — is `403 {"detail": "precheck_not_entitled"}`. This
mirrors the `secretaria_not_entitled` gate on `POST /doctor/secretaria/hub-token` (§12
table) but for the PreCheck product flag instead of secretarIA's.

**Outbound (brain-api → PreCheck, `services/precheck_handoff.py`):** only reached once
entitled. `PRECHECK_BASE_URL` or `PRECHECK_INTERNAL_TOKEN` empty ⇒ `503
{"detail": "precheck_handoff_not_configured"}` — **unlike** the doctor-portal read
proxies (§12.1), an unconfigured mesh here does **not** degrade to an empty/stub
response: a handoff that silently doesn't happen is worse than a loud failure, since
secretarIA has no other way to know the patient's session was never seeded. Otherwise:

```
POST {PRECHECK_BASE_URL}/internal/precheck-handoff
X-Internal-Token: <PRECHECK_INTERNAL_TOKEN>
{"brain_tenant_id": "<tenant_id as string>", "phone_number": "<phone_number>"}
```

Note the upstream path is **`/internal/precheck-handoff`**, NOT under `/api/v1` — a
different PreCheck router than the `/api/v1/internal/privacy/*` LGPD leg (§14) that
shares the same `PRECHECK_INTERNAL_TOKEN` credential and `X-Internal-Token` header
shape (`services/privacy.py`'s `_call_precheck` is the precedent this mirrors) but is
otherwise an unrelated surface.

**PreCheck-side contract (frozen; PreCheck implements the receiving side
independently)** — for cross-reference only, brain-api does not own this:

- `POST /internal/precheck-handoff`, header `X-Internal-Token` checked against
  PreCheck's own internal token (must equal `PRECHECK_INTERNAL_TOKEN` byte-for-byte).
- Body `{"brain_tenant_id": "<uuid string>", "phone_number": "<digits>"}`.
- `200 {"status": "seeded"}` — a fresh session was pre-created.
- `200 {"status": "already_active"}` — the patient already had a live session; no-op,
  not an error.
- `404` — no PreCheck clinic is mapped to `brain_tenant_id`.
- `409` — the patient already has a **conflicting** active session (distinct from
  `already_active`: this is a state PreCheck refuses to silently overwrite).
- `503` — PreCheck itself is degraded (e.g. its own upstream dependency down).

**Response mapping (brain-api → secretarIA; upstream body is passed through ONLY on
`200` — every error response is brain-api's OWN generic detail, never PreCheck's raw
body, mirroring `services/secretaria_internal.py`'s no-leak rule):**

| upstream (PreCheck) | brain-api response |
|---|---|
| `200 {"status": "seeded"}` | `200 {"status": "seeded"}` |
| `200 {"status": "already_active"}` | `200 {"status": "already_active"}` |
| `404` | `404 {"detail": "no_clinic_for_tenant"}` |
| `409` | `409 {"detail": "conflicting_active_session"}` |
| `503` | `503` (generic detail) |
| `422` / other `4xx` / `5xx` | `502` (generic detail) |
| network error / timeout | `502` (generic detail) |
| *(pre-upstream)* not entitled | `403 {"detail": "precheck_not_entitled"}` |
| *(pre-upstream)* mesh unconfigured | `503 {"detail": "precheck_handoff_not_configured"}` |

No DB write on this path (brain-api keeps no new state for the handoff itself). Every
outcome is logged structured with `tenant_id` + the outcome — **never** the token.

---

## 13. Billing — Stripe (billing/auth round; `stripe-billing-entitlements`)

Three concerns, never conflated: **entitlement** = the local `entitlements` row (§3;
reads never call Stripe); **billing** = Stripe (checkout, portal, subscriptions);
**metering** = future round (`usage` scaffold). On the billing path, the **webhook is
the ONLY writer** of `plan/status/addons/limits/period_*` — the client never says what
it paid. (The admin PATCH §11 remains the manual/pre-Stripe override path.)

Price ids are per-environment (`STRIPE_PRICE_MAP`: catalog id → price id); the catalog
(§3.2) stays the single source of WHAT is sellable. Stripe API calls are async httpx,
form-encoded; the `stripe` SDK is used solely for webhook signature verification.

| method | path | notes |
|---|---|---|
| `POST` | `/billing/checkout` | tenant JWT (`require_tenant`). Body `{plan, addons?: [...]}` — catalog ids only. Creates a subscription-mode Checkout Session (`tenant_id` stamped into `metadata` AND `subscription_data.metadata`; existing `stripe_customer_id` reused). `200 {url}`. `422` unknown/unassignable plan or unknown addon (plan-implied addons are silently dropped — the combo already charges for them); `503 billing_not_configured` / `price_not_configured:<id>`; `502` Stripe failure |
| `POST` | `/billing/portal` | tenant JWT. Opens the Billing Portal for the tenant's `stripe_customer_id`. `200 {url}`; `409 no_billing_account` before first checkout; `503`/`502` as above |
| `POST` | `/webhooks/stripe` | **public + signature-verified** (`Stripe-Signature` HMAC over the RAW body; unset `STRIPE_WEBHOOK_SECRET` fails CLOSED `400`). Idempotent per `event.id` (§6.3b) — replay ⇒ `200 {"received": true, "duplicate": true}`, no re-apply. The marker + mutation commit in ONE transaction; an apply error 500s so Stripe redelivers |

### 13.1 Webhook → entitlement recompute (the sole billing writer)

| event | effect on the tenant's `entitlements` row |
|---|---|
| `checkout.session.completed` | link `stripe_customer_id` + `stripe_subscription_id` (tenant from `metadata.tenant_id`/`client_reference_id`). Plan/status recompute rides the sibling `customer.subscription.*` events |
| `customer.subscription.created` / `.updated` | **full recompute**: items' price ids → catalog ids (one plan + N add-ons; add-on `quantity` scales its additive limit grants); `compute_entitlement_state` writes plan, products, full-keyset `addons`/`limits`; Stripe status maps `active/trialing/past_due/canceled` → same, anything else → `inactive` (fail closed); `period_start/end` from `current_period_*`. Unknown price ids are logged + ignored; a subscription with NO recognized plan updates status/period only (never guesses a plan) |
| `customer.subscription.deleted` | `status=canceled`, **both product flags off** (billing-managed access ends with the subscription; admin PATCH can manually re-enable) |
| `invoice.payment_failed` | `status=past_due` (tenant resolved by `customer` id) |
| `invoice.paid` | `past_due` recovers to `active` |

Tenant resolution order: our own `metadata.tenant_id` (stamped at checkout — the
trusted link) → `client_reference_id` → lookup by `stripe_customer_id`. An
unresolvable event is marked processed + logged (redelivery cannot do better).

### 13.2 Product gates ripple automatically

The webhook writing `precheck_enabled`/`secretaria_enabled` is immediately visible to:
`GET /entitlements` (§3.1 — the portal's product cards), the PreCheck SSO gate (§10 —
`403 precheck_not_entitled`), the hub-token mint + introspection (§12.2 —
`secretaria_not_entitled` / `active:false`), and `is_entitled` tier/add-on gates (§3.2).
No cache sits between the row and any gate.

---

## 14. LGPD privacy orchestration — role `admin`

Full design rationale (why three separate databases need an orchestrator, idempotency,
partial-result/retry semantics, erase-vs-anonymize per service) lives in
`docs/cross-db-erasure.md`. This section is the contract summary.

Every route here is gated by `require_role("admin")` at the router level (mirrors
§11) — a non-admin brain JWT gets `403` before any handler runs. There is no tenant
scoping: an admin can act on any subject in any tenant (the LGPD data-subject-request
use case can name any patient/user in the mesh).

| method | path | notes |
|---|---|---|
| `POST` | `/admin/privacy/erasure` | Cross-database erasure ("right to be forgotten"). Idempotent — a second run on an already-erased subject yields zero counts and still `200 completed` |
| `POST` | `/admin/privacy/export` | Cross-database export (data portability). Response carries the full aggregated bundle per service; the persisted audit row does NOT |

**Request body** (both routes, identical shape)
```json
{ "subject_type": "patient", "email": null, "wa_id": "5511999999999", "tenant_id": "2b9a…uuid", "confirm": true }
```
| field | type | rules |
|---|---|---|
| `subject_type` | enum | required; `patient` \| `user` |
| `email` | string \| null | required if `subject_type="user"`; optional additional identifier if `subject_type="patient"` |
| `wa_id` | string \| null | required if `subject_type="patient"`; ignored for `user` |
| `tenant_id` | uuid \| null | required if `subject_type="patient"`; ignored for `user` |
| `confirm` | bool | must be `true` — checked in the route (mirrors the secretaria reset guard, §11.2); `false`/missing → `400` BEFORE any local or upstream work |

A subject-combo violation (`user` with no `email`; `patient` missing `wa_id`/`tenant_id`)
is `422`. `subject_type="user"` targeting a platform `admin` user is refused
`409 admin_cannot_be_erased` (erasure only) — an admin account can never be wiped
through this path.

**Response `200`** (both routes)
```json
{
  "status": "completed",
  "brain": { "erased": { "users": 1, "demo_requests": 2 } },
  "secretaria": { "erased": { "patients": 1, "conversations": 4, "messages": 37, "appointments": 2 } },
  "precheck": { "erased": { "anamneses": 1 } },
  "request_id": "c1d2…uuid"
}
```
| field | type | notes |
|---|---|---|
| `status` | string | `completed` \| `partial` — `partial` if any upstream leg failed or was unconfigured; still `200`, never an HTTP error. The caller retries (idempotent) |
| `brain` | object | brain-api's own leg: erasure counts, or (export) the collected bundle |
| `secretaria` | object | secretaria's own JSON body, or `{"status": "failed" \| "skipped_unconfigured" \| "not_applicable"}` |
| `precheck` | object | precheck's own JSON body, or `{"status": "failed" \| "skipped_unconfigured"}` |
| `request_id` | uuid | the persisted `privacy_requests.id` (§6.3c) |

`secretaria`'s `not_applicable` marker is specific to that service: secretaria has no
notion of a brain "user" (only patients keyed by `wa_id` within a tenant, §12.1), so a
`subject_type="user"` request has nothing to send it — this is expected, NOT a
configuration problem, and does not flip `status` to `partial`.

### 14.1 Upstream contract (fixed — the other two services implement the receiving side)

- **secretaria** — `X-Internal-Api-Key` = `SECRETARIA_API_KEY` (the SAME pair key as
  §12.1), base `SECRETARIA_BASE_URL`:
  - `GET /internal/privacy/tenants/{tenant_id}/subjects/{wa_id}/export` → `200` bundle.
  - `DELETE /internal/privacy/tenants/{tenant_id}/subjects/{wa_id}` → `200
    {"erased": {"patients": n, "conversations": n, "messages": n, "appointments": n}}`.
  - Only called when the subject carries BOTH `wa_id` and `tenant_id` (i.e.
    `subject_type="patient"`); otherwise `not_applicable` (no network call).
- **precheck** — `X-Internal-Token` = `PRECHECK_INTERNAL_TOKEN` (a NEW, separate service
  credential — distinct from the forwarded-brain-JWT proxy pattern of §11.1), base
  `PRECHECK_BASE_URL`:
  - `POST /api/v1/internal/privacy/export {"email": str|null, "patient_phone": str|null}`
    → `200` bundle.
  - `POST /api/v1/internal/privacy/erase` (same body) → `200 {"erased": {...counts...}}`.
  - Always called for both subject types (`email` and/or `patient_phone`=`wa_id`, at
    least one is always present per the request validation above).

Either leg being unset locally (`SECRETARIA_BASE_URL`/`SECRETARIA_API_KEY` or
`PRECHECK_BASE_URL`/`PRECHECK_INTERNAL_TOKEN`) degrades that leg to
`skipped_unconfigured` — the rest of the orchestration still runs, and `status`
becomes `partial`. A genuinely failing upstream (network error, 4xx/5xx) degrades to
`{"status": "failed"}` the same way — no upstream response body is ever surfaced
(no leak), and no leg failure raises an HTTP error of its own.

### 14.2 Audit trail

`services/privacy.py` persists exactly ONE `privacy_requests` row (§6.3c) per
invocation, whose `result` stores counts + per-service status only — never the raw
identifier or (for export) the collected data itself. Each erasure additionally logs
`privacy_erasure_executed` at **WARNING** with the acting admin's `user_id`,
`subject_type`, and `subject_hash` — NEVER the raw email/wa_id (`docs/cross-db-erasure.md`
"identifiers only as sha256" rule).
