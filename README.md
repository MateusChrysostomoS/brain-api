# brain-api

Identity authority + BFF for the Brain platform. Owns `users`, `tenants`,
`entitlements`, and `demo_requests` in its own **`brain`** database (inside the shared
Postgres service). Faces the browser (the unified portal in `brain-frontend`).

Stack: Python 3.12, FastAPI, async SQLAlchemy 2.0 (asyncpg), Alembic, pydantic v2,
structlog, python-jose (JWT HS256), passlib[bcrypt]. Managed with `uv`.

See **[CONTRACTS.md](./CONTRACTS.md)** for the authoritative API + data contracts.

## Endpoints

| method | path | auth | purpose |
|---|---|---|---|
| `POST` | `/auth/token` | public | email+password → JWT |
| `GET` | `/auth/me` | Bearer | authenticated user + tenant identity (no secrets) |
| `GET` | `/entitlements` | Bearer | resolved product access + plan/limits (no Stripe) |
| `POST` | `/demo-requests` | public | "Agendar demo" lead capture |
| `GET` | `/health` | public | liveness |

## Local development

```bash
cp .env.example .env          # set SECRET_KEY (openssl rand -hex 64)
make up                       # local Postgres
make install                  # uv sync
make migrate                  # alembic upgrade head
make seed                     # demo tenant + owner user + entitlement
make dev                      # uvicorn on :8000
```

Seed credentials (dev only): `dra.demo@clinica.com.br` / `demo1234`.

## Deploy (Easypanel)

Build the `Dockerfile` (API service). Set `DATABASE_URL` to the shared Postgres
`brain` database, `SECRET_KEY` (shared mesh secret), and `CORS_ALLOW_ORIGINS` to the
portal origin. Run `alembic upgrade head` on release.

## Architecture notes / boundaries

- **brain-api is its own identity authority.** Its JWT carries `sub` (user UUID),
  `tenant_id`, `role` (+ an optional `professional_id`, CONTRACTS.md §16.4). Product access
  is resolved server-side via `GET /entitlements`, never carried in the token (see
  `auth-jwt-multitenant`, `stripe-billing-entitlements`).
- **SSO into PreCheck is implemented.** brain-api mints a separate, PreCheck-shaped token
  (`POST /sso/precheck/token`) for a brain user linked via `precheck_account_links`;
  PreCheck validates it with its existing, unchanged auth — see CONTRACTS.md §10.
- **Stripe billing, Google/Meta connection, and async work are all in scope now.** Stripe
  Checkout/Portal/webhook recompute is live (CONTRACTS.md §13), including a Phase 1 metered
  add-on + subscription trial (§13.3/§13.4); the onboarding round wires WhatsApp connection
  via Meta's Embedded Signup (§16.2) and a Google-Calendar-backed doctor hub through
  secretaria. Async work happens off-service: secretaria's arq crons poll
  `GET /internal/onboarding/tenants` and post back to
  `POST /internal/onboarding/tenants/{id}/events` (§16.5) — brain-api itself still has no
  Redis/arq of its own. See `docs/CHECKPOINT_onboarding_multiprofessional.md` for the full
  onboarding + multi-professional + billing-Phase-1 round, and CONTRACTS.md §15/§16 for the
  contract.
