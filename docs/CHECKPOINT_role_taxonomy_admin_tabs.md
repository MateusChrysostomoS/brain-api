# CHECKPOINT — Role taxonomy (`tenant_owner`/`tenant_staff` → `doctor`/`manager`) + admin PreCheck tabs

Status: **IMPLEMENTED, both fronts** (2026-08-07). Whole suite (from `brain-api/`):
`python -m pytest` → **422 passed**, 0 failed. Migration `0012_role_taxonomy` is written,
chain-verified, and **validated against a local Postgres** — **NOT yet run in production**
(a manual `alembic upgrade head` step, per this repo's deploy runbook). Cross-repo: all three
halves were built in the SAME 2026-08-07 session — PreCheck's dual-accept leg is **committed on
its `main`** (f9735fd code / 5fa6422 docs, unpushed), while brain-frontend's claim/guard updates
and brain-api's own changes are **uncommitted working-tree state pending review**. This
checkpoint documents brain-api's side plus the cross-repo contract.

> See `CONTRACTS.md` §2.1 (JWT claims), §6.2 (`users` table), §11/§11.1/§11.4 (admin API +
> impersonation), §12 (Doctor API, role-taxonomy intro block), §16.2/§16.3 (onboarding/invites)
> for the endpoint-level contract — this doc covers the "why", the deploy runbook, and the
> pendências.

## Why

Two independent, unrelated-on-the-surface but co-shipped changes:

1. **Admin portal tabs.** The admin "Inbound" tab was a thin proxy onto PreCheck's own
   `/api/v1/admin/inbound` (leads PreCheck itself collects). Product decision: the tab should
   instead be 100% native — brain's own `demo_requests` table, already served by
   `GET /admin/demo_requests`. That freed the admin portal to grow a genuinely useful
   cross-tenant PreCheck view instead: anamneses (list + detail) and a metrics overview,
   mirroring the shape the doctor portal already has for its OWN tenant (`GET
   /doctor/anamneses[/{id}]`) but scoped across every tenant, admin-only.
2. **Role taxonomy.** `tenant_owner`/`tenant_staff` was a flat, PreCheck-mirrored role pair that
   conflated three different things: "is this a doctor-portal user at all", "is this the person
   who owns the clinic", and "is this person also entitled to manage the clinic". Approved
   product decision: collapse to a single `doctor` role (still the base doctor-portal identity)
   plus a NEW `manager` role (full doctor access, semantically the clinic's gestor) and two
   independent booleans, `is_owner` and `is_manager`, that carry what the role string used to.

## What changed, and where (brain-api)

### 1. `models/user.py`

- `ROLE_ADMIN`/`ROLE_DOCTOR`/`ROLE_MANAGER` are the live role constants; `ROLES = (admin,
  doctor, manager)`. `ROLE_TENANT_OWNER`/`ROLE_TENANT_STAFF` kept as **legacy, read-only**
  constants (module docstring marks them so) — nothing writes them anymore, but
  `api/deps.py`/`services/admin.py` still reference them for the transition-window accept path.
- `User` gains two columns: `is_manager` (Boolean, `server_default false`) and `is_owner`
  (Boolean, `server_default false`). Doc comment on `is_owner`: "exactly one per tenant in the
  steady state … not DB-enforced".

### 2. `core/security.py` — `create_access_token`

Two new params, `is_owner: bool = False` / `is_manager: bool = False`, **always** written into
the claims dict (never omitted, unlike the pre-existing `professional_id`-only-when-set
convention) — so `api/deps.py`'s claim parsing can treat "claim absent" (a legacy token) and
"claim explicitly false" identically, with no special-casing.

### 3. `api/deps.py`

- `_LEGACY_DOCTOR_ROLES = (ROLE_TENANT_OWNER, ROLE_TENANT_STAFF)` — the transition-window accept
  list, referenced by every doctor-scoped gate below.
- `Principal` gains `is_owner: bool = False` / `is_manager: bool = False`, parsed in
  `get_current_principal` via `bool(claims.get(...))` — a missing/malformed claim reads `False`,
  **never** a 401 (the token's core identity is otherwise valid).
- `DOCTOR_ROLES = (ROLE_DOCTOR, ROLE_MANAGER)` — the new-shape doctor-portal role set.
- `require_doctor` now checks `p.role in (*DOCTOR_ROLES, *_LEGACY_DOCTOR_ROLES)`.
- `require_tenant_owner` is **renamed `require_owner`** and re-based on `p.is_owner`, with a
  legacy fallback `p.role == ROLE_TENANT_OWNER` for a pre-taxonomy token that has no
  `is_owner` claim at all.

### 4. `schemas/admin.py`

- `AdminUserOut` gains `is_manager: bool` / `is_owner: bool` (always present).
- `AdminUserCreateIn.role` becomes `Literal["admin", "doctor", "manager"]`; gains
  `is_manager: bool = False` / `is_owner: bool = False` (explicit opt-in, both default false).
  Model validator: an `admin` payload must have `is_manager`/`is_owner` both false (and no
  `tenant_id`); a `doctor`/`manager` payload requires a `tenant_id`.

### 5. `services/admin.py`

- `_LEGACY_DOCTOR_ROLES` re-declared here too (used by `issue_impersonation_token`'s target-role
  check).
- `create_user`: effective `is_manager` is forced `True` when `payload.role == ROLE_MANAGER`
  regardless of the payload's own flag (a pure `manager` role is trivially "a manager" —
  mirrors a PreCheck idiom); any other role takes the payload's flag verbatim. `is_owner`
  always takes the payload's flag verbatim (no forcing) — owner via admin tooling is a
  deliberate act.
- `issue_impersonation_token`: target-role check widened to `(ROLE_DOCTOR, ROLE_MANAGER,
  *_LEGACY_DOCTOR_ROLES)`; the minted token now carries `is_owner=user.is_owner,
  is_manager=user.is_manager` read straight off the target row.

### 6. `services/signup.py`

Cold-signup owner provisioning now creates the owner `User` with `role=ROLE_DOCTOR,
is_owner=True, is_manager=True` (was `role=tenant_owner`) — "the person who bought the clinic
is trivially also its manager".

### 7. `services/onboarding_sync.py` — `get_owner`

The shared "find the tenant's owner user" helper (used by the secretaria-provisioning bridge and
`api/onboarding.py`'s connection-success email) now queries `User.is_owner.is_(True)` instead of
a role-string comparison.

### 8. `api/onboarding.py` — professional invites

`POST /doctor/professionals/invites` now creates the invited local `User` with
`role=ROLE_DOCTOR` (was `tenant_staff`); `is_owner`/`is_manager` are left at their column
default (`False`) — an invited professional is neither by default. Both invite routes
(`/invites`, `/professionals/self`) stayed on plain `require_doctor` (unaffected by this round —
that gate loosening was the 2026-07-22 corrections round, a separate prior change). The
owner-only `pause` route now depends on the renamed `require_owner`.

### 9. `api/admin.py` — the tab swap

- `GET /admin/inbound` **removed** entirely (no route, no handler).
- Three new routes added under a new "PreCheck admin proxies (anamneses + metrics)" block,
  same router-level `require_role("admin")` gate as everything else in this file:
  - `GET /admin/anamneses` (`list_admin_anamneses`) — cross-tenant list, `skip`/`limit`.
  - `GET /admin/anamneses/{anamnesis_id}` (`get_admin_anamnesis`) — detail.
  - `GET /admin/metrics` (`get_admin_metrics`) — `days` (default 30) + `all` (aliased query
    param `all`, since `all` shadows a builtin).
- Each forwards the caller's brain JWT verbatim to PreCheck (same pattern as the existing
  doctor-portal anamneses proxy) — no second credential; PreCheck re-validates and re-checks the
  `admin` role on its own side. Cross-tenant: unlike `/doctor/anamneses` (scoped to the caller's
  tenant), these see every tenant, gated only by the caller's brain `admin` role.

### 10. `services/precheck_client.py`

Three new thin proxy functions: `list_admin_anamneses`, `get_admin_anamnesis`,
`get_admin_metrics`, all built on the existing `_proxy_get` helper (same 4xx-passthrough /
5xx-collapses-to-502 / never-logs-the-bearer-token behavior as the pre-existing doctor-anamneses
proxies). Fallback shape differs by endpoint:
- `list_admin_anamneses`: unconfigured `PRECHECK_BASE_URL` → `_empty_page(skip, limit)`
  (`{"items": [], "total": 0, "skip", "limit", "stub": true}`).
- `get_admin_anamnesis`: **no fallback** — always hits `_proxy_get`, so unconfigured →
  `503 precheck_not_configured` (matches the existing doctor-detail proxy's behavior — a detail
  view has no sensible "empty" shape).
- `get_admin_metrics`: unconfigured → `{"stub": true}` (not a `Page` — this endpoint was never
  paginated).

### 11. `migrations/versions/0012_role_taxonomy.py` (revises `0011_launch_waitlist`)

`upgrade()`: adds `users.is_manager` / `users.is_owner` (both `server_default false`,
`nullable=False`), then two `UPDATE` statements — `tenant_owner` rows become `role='doctor',
is_owner=true, is_manager=true`; `tenant_staff` rows become `role='doctor'` (booleans left at
their default `false/false`, "a purely additive downgrade of privilege boundary, since `manager`
already carried nothing `doctor` didn't"). `downgrade()` is documented as **best-effort**: a
`doctor` row created fresh under the new taxonomy cannot be told apart from one that came from a
backfilled `tenant_owner`/`tenant_staff` row, so downgrading loses any `is_manager` grant made
to a non-owner doctor after this migration ran.

## Tests

`python -m pytest -q` from `brain-api/` → **422 passed**, 0 failed (whole suite). The role
constants/flags are exercised across the existing auth, admin, doctor, onboarding, and
impersonation test modules (no dedicated new test file for this round — the change is additive
to existing coverage, not a new vertical). Migration verified offline against a local Postgres
(chain applies cleanly, columns + backfill match the model), separately from the pytest run.

## Cross-repo contract

This is a 3-repo, sequenced change, all three halves built in the same 2026-08-07 session.
PreCheck's half is committed on its `main` (unpushed); brain-frontend's half is uncommitted
working-tree state, like this repo's. Verified by reading their code while writing this doc:

- **PreCheck** (`app/core/brain_auth.py`): `BRAIN_DOCTOR_ROLES = ("doctor", "manager",
  "tenant_owner", "tenant_staff")` — PreCheck's own brain-JWT validator already dual-accepts
  every role shape brain-api can mint, old or new, for `/doctor/*`-equivalent PreCheck routes
  (e.g. the SSO'd anamnesis views) and the new admin proxies' upstream targets
  (`/api/v1/admin/anamneses[/{id}]`, `/api/v1/admin/metrics`) alike, since it revalidates the
  forwarded brain JWT itself rather than trusting brain-api's gate.
- **brain-frontend**: `session.isOwner`/`session.role` (and `is_manager` on the `/doctor/me` /
  admin-users payloads) are read directly; every `usePortalGuard(...)` call across the doctor
  portal is already widened to `["doctor", "manager", "tenant_owner", "tenant_staff"]`; every
  owner-only UI branch checks `session.isOwner || session.role === "tenant_owner"` (the same
  claim-first-legacy-fallback pattern as backend `require_owner`); the admin users screen
  (`app/(site)/admin/users/page.tsx`) shows `ROLE_LABEL`/`ROLE_TONE` fallback entries for legacy
  `tenant_owner`/`tenant_staff` rows and renders an `is_owner` badge + an "Também é gestor"
  (`is_manager`) checkbox on create.

**Invariant that makes the sequencing safe:** none of the three repos requires a coordinated
instant cutover. brain-api mints `is_owner`/`is_manager` unconditionally once its own code is
live (independent of whether migration `0012` has run — the booleans just read `false` off
still-legacy rows until the backfill executes); PreCheck and brain-frontend both already accept
either shape. The only genuinely ordered step is deploying brain-api's *code* (which accepts
both shapes) before running the migration (which rewrites the data) — see Runbook below.

## Runbook (deploy order)

Migrations in this repo are a **manual** `alembic upgrade head` step, never automatic on
release (per `migrations/versions/0012_role_taxonomy.py`'s own docstring and this repo's
existing convention).

1. **Deploy PreCheck** — already done; its dual-accept (`BRAIN_DOCTOR_ROLES`) is live, so it
   will accept brain-api tokens minted either before or after step 2 below.
2. **Deploy brain-api** (this round's code) — mints `doctor`/`manager` + `is_owner`/`is_manager`
   claims from the moment it's live; still ACCEPTS legacy `tenant_owner`/`tenant_staff` tokens
   for their remaining TTL (`_LEGACY_DOCTOR_ROLES` in `api/deps.py` / `services/admin.py`).
   `users.role` values in the DB are still pre-taxonomy at this point — fine, `require_doctor`
   accepts both shapes.
3. **Run `alembic upgrade head`** (`0011_launch_waitlist` → `0012_role_taxonomy`) — MANUAL.
   Adds the two columns and backfills every `tenant_owner`/`tenant_staff` row. Safe to run any
   time after step 2 (never before — the migration assumes the code that reads the new columns
   is already live) and does not require downtime (additive columns + in-place `UPDATE`s).
4. **Deploy brain-frontend** — already using `isOwner`/`session.role` with legacy fallbacks, so
   it works correctly both before and after step 3; deploying it is not itself ordered relative
   to the others, just listed last for completeness.
5. **Transition window**: any access token minted by the OLD brain-api build before step 2 keeps
   authenticating for its remaining lifetime — `ACCESS_TOKEN_EXPIRE_MINUTES` (default **30**).
   After that window closes, no `tenant_owner`/`tenant_staff` token can still be in circulation.

No rollback step is needed if step 3 is skipped or delayed: `require_doctor`/`require_owner`
keep working against un-migrated rows via the legacy fallback indefinitely (it just never turns
itself off on its own — see Pendências).

## Pendências

- **Migration `0012_role_taxonomy` has not been run in production.** Steps 1–2 above (PreCheck,
  brain-api) can ship independently of this; step 3 is a manual follow-up.
- **Remove the legacy acceptance paths** once the transition window has long passed (i.e. once
  every pre-deploy token has expired AND migration `0012` has run in production):
  `_LEGACY_DOCTOR_ROLES` + the `require_owner` role-string fallback in brain-api's
  `api/deps.py`/`services/admin.py`; `BRAIN_DOCTOR_ROLES`'s legacy tuple entries in PreCheck's
  `app/core/brain_auth.py`; the `tenant_owner`/`tenant_staff` guard entries and
  `ROLE_LABEL`/`ROLE_TONE` fallback rows across brain-frontend's `usePortalGuard(...)` call
  sites and admin/doctor-perfil/secretaria pages. None of this is urgent — every fallback is
  inert once no legacy data/tokens remain — but it's dead code worth sweeping in one pass later.
- **The admin "create user" UI does not expose `is_owner`.** `POST /admin/users` accepts it
  (`AdminUserCreateIn.is_owner`), but brain-frontend's create-user form
  (`app/(site)/admin/users/page.tsx`) only ever posts `is_manager`; `is_owner` can only be set
  today via a direct API call. The table DOES render an `is_owner` badge for existing rows.
- **A "manager puro" (role=`manager`, not also an owner) has no dedicated tenant-metrics screen
  yet.** `manager` gets every `require_doctor` gate today (same access as `doctor`), but no
  admin/product decision has carved out anything manager-specific beyond that — it's currently
  just "doctor access under a different label with a semantically distinct badge".
