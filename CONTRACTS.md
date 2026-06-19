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
(forwarding the caller's brain JWT). `secretaria` remains uncalled (its `/doctor`
appointments/patients are stubs); no browser ever calls `precheck`/`secretaria` directly.

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
{ "access_token": "<jwt>", "token_type": "bearer" }
```
> Shape intentionally identical to PreCheck's `TokenResponse` and the frontend's
> existing `LoginResponse` type, so the client stores `data.access_token` unchanged.

**Status codes**
- `200` success
- `401` `{"detail": "Credenciais inválidas"}` — unknown email OR bad password
  (do **not** distinguish the two; same message, constant-time-ish path)
- `422` validation error (malformed email, password > 72 bytes)
- `429` `{"detail": "..."}` — rate limited (basic, optional; see §5)

**JWT claims** (HS256, `SECRET_KEY`, pinned `algorithms=["HS256"]`)
| claim | value |
|---|---|
| `sub` | brain user id, UUID **string** |
| `tenant_id` | tenant UUID **string**, or `null` for a platform `admin` |
| `role` | `"admin"` \| `"tenant_owner"` \| `"tenant_staff"` |
| `iat` | issued-at (UTC) |
| `exp` | `iat + ACCESS_TOKEN_EXPIRE_MINUTES` (default 60) |

No entitlements, no secrets, no plan flags in the token (skill rule).

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
  "plan": "brain-completo",
  "status": "active",
  "addons": {},
  "limits": {},
  "usage": {}
}
```
| field | type | notes |
|---|---|---|
| `tenant_id` | uuid string | from the token |
| `clinic_name` | string | from `tenants.clinic_name` |
| `products.precheck` | bool | `entitlements.precheck_enabled` |
| `products.secretaria` | bool | `entitlements.secretaria_enabled` |
| `plan` | string | e.g. `"brain-completo"` \| `"precheck"` \| `"secretaria"` \| `"free"` |
| `status` | string | `active` \| `trialing` \| `past_due` \| `canceled` \| `inactive` |
| `addons` | object | **MVP: `{}`** (flags scaffolded, all false/empty) |
| `limits` | object | per-feature limits scaffold, e.g. `{}` or `{"messages": 1000}` |
| `usage` | object | usage counters scaffold, zeros, e.g. `{}` or `{"messages": 0}` |

**Resolution rules**
- If no `entitlements` row exists for the tenant, return a **default**: `products` both
  `false`, `plan: "free"`, `status: "inactive"`, `addons/limits/usage: {}`. (Never 404
  for a valid tenant — the portal must always render a coherent state.)
- `admin` principals (no `tenant_id`): respond `409 {"detail": "No tenant in context"}`
  (the unified portal logs in as a tenant user; admin uses other tooling).

**Status codes:** `200`; `401` missing/invalid token; `409` token has no tenant.

> **Frontend mapping** (`lib/manage-api.ts` `getEntitlements()` → existing
> `Entitlements` type `{ precheck, secretaria, plan, clinicName }`):
> `precheck = products.precheck`, `secretaria = products.secretaria`,
> `plan = plan`, `clinicName = clinic_name`. The `/app` shell consumes those four
> fields unchanged — no dashboard rewrite.

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
- `POST /auth/token`: optional modest IP limit (e.g. 10 / minute) to blunt credential
  stuffing. Not required for correctness.

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
| `plan` | String(32) | not null, server_default `'free'` |
| `status` | String(32) | not null, server_default `'inactive'` |
| `addons` | JSON | not null, server_default `'{}'` (all false/empty for MVP) |
| `limits` | JSON | not null, server_default `'{}'` |
| `usage` | JSON | not null, server_default `'{}'` |
| `period_start` | DateTime(tz) | nullable |
| `period_end` | DateTime(tz) | nullable |
| `stripe_customer_id` | String(64) | nullable, indexed (scaffold; unused this task) |
| `stripe_subscription_id` | String(64) | nullable (scaffold) |
| `updated_at` | DateTime(tz) | server_default now(), onupdate now() |

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

Migration **`0001`** creates `tenants`/`users`/`entitlements`/`demo_requests`; migration
**`0002`** adds `precheck_account_links`.

---

## 7. Configuration / env vars (brain-api `.env`)

| var | default | purpose |
|---|---|---|
| `APP_ENV` | `dev` | env name (`dev`/`staging`/`production`) |
| `LOG_LEVEL` | `INFO` | structlog level |
| `DATABASE_URL` | `postgresql+asyncpg://…/brain` | the **brain** database |
| `SECRET_KEY` | — | JWT HS256 signing key. **MUST be byte-identical to the PreCheck backend's `SECRET_KEY`** — the minted SSO token (§10) is only valid if both services share it |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | brain access-token TTL |
| `PRECHECK_TOKEN_EXPIRE_MINUTES` | `60` | TTL of the minted PreCheck SSO token (§10); matches PreCheck's own session length |
| `CORS_ALLOW_ORIGINS` | `http://localhost:3000` | comma-separated portal origins |
| `DEMO_RATE_LIMIT_PER_MIN` | `5` | basic demo-request anti-spam |
| `ADMIN_EMAIL` | `""` | platform admin bootstrap (`scripts/seed_admin.py`); env-only, never in code |
| `ADMIN_PASSWORD` | `""` | platform admin bootstrap; bcrypt-hashed on insert, never logged |
| `PRECHECK_BASE_URL` | `""` | PreCheck backend base URL for the BFF read proxy (§11.1). Empty → list proxies return an empty page locally |
| `PRECHECK_TIMEOUT_SECONDS` | `10` | timeout for the precheck proxy httpx client |
| `SECRETARIA_BASE_URL` | `""` | reserved; secretaria appointments/patients are stubs until it exposes them |
| `INTERNAL_API_KEY` | `""` | reserved: `X-Internal-Api-Key` for the future brain-api → secretaria call (§12) |

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
| `models/entitlement.py`, `services/entitlements.py`, `GET /entitlements` | `stripe-billing-entitlements` |
| `schemas/*Out` whitelists (no `*_encrypted`/`password_hash`), `core/logging.py` `redact_secrets` | `tenant-secrets-encryption` |
| `POST /demo-requests` (sync, isolated) | n/a — `whatsapp-webhook-arq` explicitly skipped (no async work) |
| `core/security.py create_precheck_token`, `services/sso.py`, `api/sso.py` | `auth-jwt-multitenant` (pinned HS256, minimal claims, short TTL, shared secret) |
| `services/sso.py` entitlement gate (reuses `resolve_entitlement`) | `stripe-billing-entitlements` (in-process read, never Stripe) |
| `models/precheck_link.py` (identity map; never serialized/logged) | `tenant-secrets-encryption` (never-leak posture) |

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
  with 401. This is the **only** coupling; no PreCheck code changed.
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
| `PATCH` | `/admin/tenants/{tenant_id}/entitlements` | partial `{precheck_enabled?, secretaria_enabled?, plan?, status?, addons?, limits?}`; **upserts** the row; `404` unknown tenant. How a product is manually switched on pre-Stripe |
| `GET` | `/admin/users` | `Page` of `{id, tenant_id, clinic_name|null, email, name, role, created_at}`. **Never** `password_hash` |
| `POST` | `/admin/users` | `201` create in any tenant/role. Body `{email, name, password(1–72), role, tenant_id?}`. `admin` ⇒ `tenant_id` must be null; tenant roles ⇒ `tenant_id` required+existing. `409` dup email, `404` unknown tenant, `422` bad combo / >72-byte password |
| `GET` | `/admin/demo_requests` | `Page` of brain's own demo leads, newest first |
| `PATCH` | `/admin/demo_requests/{id}` | set `status ∈ {contacted, converted, dismissed}`; `404` unknown, `422` other value. (Portal actions "Marcar como contatado" / "Converter em tenant" / "Descartar") |
| `GET` | `/admin/inbound` | **proxy** → PreCheck `GET /api/v1/admin/inbound` (§11.1); returns PreCheck's payload verbatim |

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
| `GET` | `/doctor/appointments` | **stub** `{"data": [], "stub": true}` — secretaria has no internal endpoint yet (then brain-api → secretaria via `X-Internal-Api-Key`, scoped to the tenant) |
| `GET` | `/doctor/patients` | **stub** `{"data": [], "stub": true}` (same as appointments) |
| `GET` | `/doctor/anamneses` | **proxy** → PreCheck `GET /api/v1/doctor/anamneses` (§11.1); tenant-scoped by the forwarded token |
| `GET` | `/doctor/anamneses/{id}` | **proxy** → PreCheck `GET /api/v1/doctor/anamneses/{id}`; PreCheck enforces the record belongs to the token's tenant/clinic |
