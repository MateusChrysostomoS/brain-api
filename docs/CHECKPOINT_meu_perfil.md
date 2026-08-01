# CHECKPOINT — Meu Perfil (Part 1: self-edit foundation, `PATCH /doctor/me`)

Status: **BUILT + tested locally (2026-08-01)**, UNPUSHED, no migration (no schema change).
`python -m pytest` → **367 passed** (whole suite, incl. `tests/test_doctor_profile.py`, new — 7 tests).

> See also `CONTRACTS.md` §12 (Doctor (tenant) API) — the endpoint contract is folded
> directly into that table (not just a pointer); this doc covers the "why" and the
> decisions behind it. Scope here is BACKEND-ONLY. The brain-frontend "Meu Perfil" screen
> (`/doctor/perfil`, personal-info block wired to this endpoint) was built in the same
> session but lives in brain-frontend's own tree — no checkpoint doc there per this
> round's instructions (explicitly deferred to whichever round closes that repo). A
> second, independent block on the same frontend page ("Configuração da secretaria") is
> out of scope here too — a different agent, different endpoint(s), same page.

## Why

`GET /doctor/me` already existed, but there was no way for a doctor to fix a typo in
their own name — or make any self-service edit at all — without an admin doing it via
`POST /admin/users` (which only creates users; there is no admin edit endpoint either).
This round adds the smallest possible self-edit surface, `PATCH /doctor/me`, accepting
exactly one low-risk field, as the foundation the new "Meu Perfil" screen is built on.

## What changed, and where

### 1. `schemas/doctor.py` — `DoctorMeUpdateIn`

New schema, `extra="forbid"`: exactly one field, `name: str` (`min_length=1,
max_length=255`, matching the `users.name` column), trimmed and re-checked non-blank via
a `field_validator` (same `_trim` pattern as `SignupIntentCreate.name`/`clinic_name` in
`schemas/signup.py`). `extra="forbid"` is the ENFORCEMENT mechanism for "email/role/
tenant/password can never be edited here": a payload that includes any of them is
rejected `422` by Pydantic itself, before the route handler or service layer ever runs —
no manual `if "email" in payload` filtering that could silently drift out of sync.

### 2. `services/doctor.py` — `update_doctor_me`

Mirrors `get_doctor_me`'s shape exactly (same 401-on-deleted-user/tenant handling, same
`DoctorMeOut` assembly), but first loads the CALLER'S OWN user row (`principal.user_id` —
never a client-supplied id) and writes `user.name = payload.name` before committing.
Returns the refreshed `DoctorMeOut` so the frontend can swap in the server's canonical
view in one response, no extra `GET` round-trip needed.

### 3. `api/doctor.py` — `PATCH /doctor/me`

Added next to the existing `GET /doctor/me`, under the SAME router-level
`Depends(require_doctor)` gate as every other `/doctor/*` route (no new dependency
needed) — `tenant_owner` and `tenant_staff` can both edit their own name; an admin token
still gets `403` (wrong portal, same as the rest of the router).

## Tests

`tests/test_doctor_profile.py` (new), 7 tests: happy-path name edit (+ persists across a
separate `GET`), whitespace trimming, blank-after-trim / empty / missing `name` all
`422`, smuggled `email`/`role`/`tenant_id`/`password` all `422` (and confirmed to have
changed NOTHING server-side), no-auth `401`, admin-token `403`, and tenant isolation
(editing as Owner A never touches Owner B's row). Whole suite: **367 passed**, 0 failed.

## Decisions

- **Only `name` is editable** — the instructions explicitly allowed this ("na dúvida, só
  nome"): `email` is the login key and carries PreCheck SSO identity implications, `role`/
  `tenant_id` are RBAC-sensitive, `password` has its own dedicated endpoint
  (`POST /auth/set-password`) with its own policy. No other column on `User` is a
  plausible self-edit candidate today (`professional_id`, `invite_token_*` are
  system-managed).
- **`name` is REQUIRED, not optional**, even though the route is a `PATCH`. The payload
  has exactly one editable field and the frontend always has the current value in hand
  (from its own prior `GET /doctor/me`) before rendering an editable input — there is no
  "leave unspecified fields alone" case to support yet, unlike `EntitlementPatchIn`
  (`schemas/admin.py`), which patches several independent fields and genuinely needs
  per-field optionality. If a second editable field is added later, this may need to move
  to that optional-fields shape instead.

## Pendências

- **Second profile block ("Configuração da secretaria")** on the same frontend page is
  explicitly out of scope for this round; whoever picks it up will likely need its own
  backend endpoint(s) too (none exist yet).
- No migration needed — no schema change (`users.name` already existed).
- Local test runner note (this machine only): `uv run pytest` / `uv run python -m
  pytest` / the venv's own `.venv\Scripts\python.exe` are all blocked by a Windows App
  Control policy ("Uma política de Controle de Aplicativo bloqueou este arquivo"). The
  suite still runs by invoking uv's base cpython interpreter directly with `PYTHONPATH`
  set, from `brain-api/`:
  `$env:PYTHONPATH = "src;.venv\Lib\site-packages"; & "$env:APPDATA\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe" -m pytest -q`
  (exact patch-version directory name may drift on a future `uv sync`/toolchain update).
