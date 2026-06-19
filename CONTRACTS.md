# brain-api — API & Data Contracts (Phase 1)

> **Status:** authoritative. Phase 2 (backend) and Phase 3 (frontend) build against
> THIS document. If an implementation detail disagrees with this file, this file wins
> until it is amended here first.

`brain-api` is the **identity authority + BFF** for the Brain platform. It owns
`users`, `tenants`, `entitlements`, and `demo_requests` inside its own **`brain`
database** in the shared Postgres service. It faces the browser (the unified portal in
`brain-frontend`). `secretaria` and `precheck` remain internal-only services and are
**not** called by anything in this task.

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

2. **SSO into PreCheck is OUT OF SCOPE (deferred).** PreCheck mints its own minimal
   token (`precheck_token`, claims `sub`=*integer* user id + `exp`, pbkdf2 passwords,
   its own `users` table). A brain-api JWT is **not** a drop-in `precheck_token`
   (UUID vs int `sub`, different user store), even though both use `SECRET_KEY`/HS256.
   Reconciling them (unified SSO that hands `/dashboard` a valid PreCheck token) is the
   follow-up the previous run flagged — it touches PreCheck's backend contract and is
   **not** done here. The PreCheck product dashboard (`/dashboard`) keeps its own login.

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

One **single initial Alembic migration** creates all four tables.

---

## 7. Configuration / env vars (brain-api `.env`)

| var | default | purpose |
|---|---|---|
| `APP_ENV` | `dev` | env name (`dev`/`staging`/`production`) |
| `LOG_LEVEL` | `INFO` | structlog level |
| `DATABASE_URL` | `postgresql+asyncpg://…/brain` | the **brain** database |
| `SECRET_KEY` | — | JWT HS256 signing key (shared mesh secret) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | access-token TTL |
| `CORS_ALLOW_ORIGINS` | `http://localhost:3000` | comma-separated portal origins |
| `INTERNAL_API_KEY` | `""` | reserved for future service-to-service (unused this task) |
| `DEMO_RATE_LIMIT_PER_MIN` | `5` | basic demo-request anti-spam |

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

- Login flow: existing design (`AuthShell` + `PasswordField`) → `login()` → store JWT in
  the brain session (`brain.session`, sessionStorage — the key `/app` already reads) →
  route to `/app` → `getEntitlements()` → conditionally show/link PreCheck + SecretarIA.
- The **invented standalone login screen does not exist** in the current tree (verified)
  — nothing to remove; the existing `/login` design is the one we keep/wire.
- `ContactForm.handleSubmit` calls `submitDemoRequest()` and shows the existing success
  state on `201`.

---

## 9. Skill compliance map (which skill governs each backend module)

| module | skill |
|---|---|
| `core/security.py` (JWT mint/verify, bcrypt), `api/deps.py` (Principal, role/tenant scope) | `auth-jwt-multitenant` |
| `models/entitlement.py`, `services/entitlements.py`, `GET /entitlements` | `stripe-billing-entitlements` |
| `schemas/*Out` whitelists (no `*_encrypted`/`password_hash`), `core/logging.py` `redact_secrets` | `tenant-secrets-encryption` |
| `POST /demo-requests` (sync, isolated) | n/a — `whatsapp-webhook-arq` explicitly skipped (no async work) |
