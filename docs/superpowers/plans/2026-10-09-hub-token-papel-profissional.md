# Hub token — quem vê a agenda inteira (`agenda_scope`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The secretarIA hub-token introspection (`POST /internal/secretaria/hub-token/verify`) also answers `agenda_scope: "clinic" | "own"` — whether the person behind the hub session sees every professional's agenda (owner/manager, secretary) or only their own (any other doctor) — computed LIVE from the acting user's row, so secretarIA can enforce the owner's agenda-visibility rule in its backend.

**Architecture:** A pure rule `services/hub_scope.py::agenda_scope_for(role, is_owner, is_manager)` plus a live resolver `resolve_agenda_scope(session, tenant_id, actor)` that loads the user named by the hub token's `act` claim (fail closed to `"own"` for an unknown actor, a malformed id or a user of another tenant). The verify endpoint adds the field; nothing about the token, its mint, `active`, `tenant_id` or `professional_id` changes. Additive response field: secretarIA parses this body permissively (`body.get(...)`, no strict schema), so this deploy is a no-op for today's secretarIA and must go live BEFORE the secretarIA build that reads it (see Global Constraints).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Pydantic v2, pytest (in-memory SQLite), uv, ruff.

**Spec:** `C:\TECH\BRAIN\secretarIA\docs\superpowers\specs\2026-10-09-acoes-clinica-avisos-paciente-design.md` §5.A (owner decision 2026-10-09). Consumer plan: `C:\TECH\BRAIN\secretarIA\docs\superpowers\plans\2026-10-09-lembretes-r7-avisos-acoes-clinica.md` (Task 13 reads the field; its "Produces for R5" section is what the front sees).

**Cross-plan order:** this plan FIRST → R7 (secretarIA) → R5 (Brain-Message-Frontend). **Deploy order (no plan deploys anything):** secretarIA migration `d8e3a5c1f7b2` → **brain-api (this plan)** → `secretaria_api` + `secretaria-worker` together → front.

**Code base (verified 2026-10-09):** `brain-api` `main` at `b1a289a`. Ground truth read: `src/brain_api/api/internal.py::verify_hub_token`, `src/brain_api/schemas/internal.py::HubTokenVerifyOut` (`active`, `tenant_id`, `professional_id` — a RESPONSE model, no `extra="forbid"` concern), `src/brain_api/core/security.py::create_hub_token` (claims `sub`=tenant, `scope`, `act`=acting user id, optional `professional_id`), `src/brain_api/api/deps.py` (`Principal`, `require_doctor`, `deny_secretary`, `is_billing_manager`), `src/brain_api/models/user.py` (`ROLE_DOCTOR`, `ROLE_MANAGER`, `ROLE_SECRETARY`, legacy `ROLE_TENANT_OWNER`/`ROLE_TENANT_STAFF`, `is_owner`, `is_manager`, `professional_id`). secretarIA side: `secretarIA/src/secretaria/core/subscription.py::verify_subscription_token` reads the body with `body.get(...)` and ignores unknown keys.

**Execution context:** worktree `C:\TECH\BRAIN-worktrees\TASK-044\brain-api` from `origin/main`, branch `task/TASK-044-hub-token-papel` (TASK-044 is the cross-repo task of R7; register the worktree in `C:\TECH\BRAIN\tasks\TASK-044\TASK.md`).

## Global Constraints

- brain-api is the identity authority: the rule lives here, secretarIA never re-derives a role. The answer is read LIVE from `users` at every introspection (auth-jwt-multitenant: mutable role state is never trusted from a token) — a role change takes effect within secretarIA's positive-cache TTL (`SUBSCRIPTION_CACHE_TTL_SECONDS`, default 60 s), old tokens included.
- Rule (owner decision 2026-10-09 A): `secretary` → `"clinic"`; `manager` → `"clinic"`; `doctor` with `is_owner` or `is_manager` → `"clinic"`; LEGACY `tenant_owner` → `"clinic"`; everything else (plain `doctor`, LEGACY `tenant_staff`, `admin`, unknown role, unknown/malformed actor, actor of another tenant) → `"own"`. Fail closed.
- Additive and backward compatible: `agenda_scope: Literal["clinic", "own"] = "own"` on `HubTokenVerifyOut`; `active`, `tenant_id`, `professional_id`, the token format and the mint are unchanged; a refused or invalid token answers `"own"` (default).
- Contract direction (frozen-contract-migration): the producer (brain-api) adds the field first; the consumer (secretarIA) parses permissively, so this deploy alone changes nothing visible. secretarIA tolerates its absence (fail closed for a token carrying a professional, R7 Task 13). Wrong order symptom to recognise: a manager who is also a doctor sees only their own appointments and no "Todos / Só os meus" switch — deploy brain-api.
- No new log line carries a name, e-mail or token; ids and the scope value only.
- Validation: `make test` / `make lint` (= `uv run pytest`, `uv run ruff check .` + `uv run ruff format --check .`). If App Control blocks the venv interpreter, run the same with the base interpreter from Git Bash: `PYTHONPATH="src;.;.venv/Lib/site-packages" /c/Users/mateu/AppData/Roaming/uv/python/cpython-3.12-windows-x86_64-none/python.exe -m pytest <args>`. Never `ruff format .` on the whole repo; format only files this plan creates.
- Commit with `git add <explicit paths>` (never `git add -A`); every message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. **Never push, merge or deploy.**

## Produces for secretarIA (exact wire shape)

`POST /internal/secretaria/hub-token/verify` (unchanged request, `X-Internal-Api-Key`) → `200`:

```json
{"active": true, "tenant_id": "<uuid>", "professional_id": "<uuid>" , "agenda_scope": "clinic"}
```

- `agenda_scope`: `"clinic"` = sees every professional's appointments; `"own"` = only the appointments of `professional_id` (none when `professional_id` is null). Always present from this version on; `"own"` on every refused/invalid token.
- `professional_id`: unchanged (from the token claim; null when absent/malformed).

## Review Focus

Each line is pinned by a test in the task named in brackets.

1. A plain doctor (no owner/manager flag) is `"own"`; a doctor promoted to manager AFTER the hub token was minted is `"clinic"` on the next introspection (live, not from the token) [Tasks 1, 2].
2. The hub token's `act` names a user of ANOTHER clinic, a deleted user, or is not a UUID → `"own"`, never `"clinic"` [Tasks 1, 2].
3. A secretary is `"clinic"` even with both flags false (product decision: the receptionist runs every agenda) [Tasks 1, 2].
4. A refused token (user JWT, garbage, inactive entitlement) still answers `200` with `active:false` and `agenda_scope:"own"` — the field never turns a refusal into an error [Task 2].
5. The existing `professional_id` passthrough and `active` semantics are byte-for-byte unchanged (old tests stay green) [Task 2].

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/brain_api/services/hub_scope.py` | create | the rule (`agenda_scope_for`) and the live resolver (`resolve_agenda_scope`) |
| `src/brain_api/schemas/internal.py` | modify | `HubTokenVerifyOut.agenda_scope` |
| `src/brain_api/api/internal.py` | modify | `verify_hub_token` answers it |
| `tests/test_hub_agenda_scope.py` | create | rule, resolver, endpoint |
| `CONTRACTS.md` | modify | §12.2 introspection answer |
| `docs/CHECKPOINT_hub_token_agenda_scope.md` | create | state, rule, deploy order |
| `CLAUDE.md` | modify | one pointer line |

---

## Task 1: The rule and the live resolver

**Files:**
- Create: `src/brain_api/services/hub_scope.py`
- Test: `tests/test_hub_agenda_scope.py`

**Interfaces:**
- Consumes: `brain_api.models.User`; role constants from `brain_api.models.user`.
- Produces (in `brain_api.services.hub_scope`): `AGENDA_SCOPE_CLINIC = "clinic"`, `AGENDA_SCOPE_OWN = "own"`; `def agenda_scope_for(role: str, *, is_owner: bool, is_manager: bool) -> str`; `async def resolve_agenda_scope(session: AsyncSession, tenant_id: UUID, actor: object) -> str` (never raises for a bad `actor`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hub_agenda_scope.py`:

```python
"""Who sees the whole clinic agenda in secretarIA's hub (owner decision 2026-10-09 A).

Ground truth: services/hub_scope.py (rule + live resolver), api/internal.py::verify_hub_token.
"""

from uuid import uuid4

import pytest

from brain_api.core.security import hash_password
from brain_api.models import Tenant, User
from brain_api.services.hub_scope import (
    AGENDA_SCOPE_CLINIC,
    AGENDA_SCOPE_OWN,
    agenda_scope_for,
    resolve_agenda_scope,
)

# --- the rule ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "is_owner", "is_manager", "expected"),
    [
        ("secretary", False, False, AGENDA_SCOPE_CLINIC),
        ("secretary", True, True, AGENDA_SCOPE_CLINIC),
        ("manager", False, False, AGENDA_SCOPE_CLINIC),
        ("doctor", True, False, AGENDA_SCOPE_CLINIC),
        ("doctor", False, True, AGENDA_SCOPE_CLINIC),
        ("doctor", False, False, AGENDA_SCOPE_OWN),
        ("tenant_owner", False, False, AGENDA_SCOPE_CLINIC),  # legacy token window
        ("tenant_staff", False, False, AGENDA_SCOPE_OWN),  # legacy token window
        ("admin", True, True, AGENDA_SCOPE_OWN),
        ("", False, False, AGENDA_SCOPE_OWN),
        ("Doctor", True, True, AGENDA_SCOPE_OWN),  # roles are exact lower-case strings
    ],
)
def test_the_rule(role, is_owner, is_manager, expected):
    assert agenda_scope_for(role, is_owner=is_owner, is_manager=is_manager) == expected


# --- the live resolver ---------------------------------------------------------------


async def _user(session, tenant_id, *, role="doctor", is_owner=False, is_manager=False) -> User:
    user = User(
        tenant_id=tenant_id,
        email=f"{uuid4().hex}@x.com",
        name="Pessoa",
        password_hash=hash_password("senha1234"),
        role=role,
        is_owner=is_owner,
        is_manager=is_manager,
    )
    session.add(user)
    await session.flush()
    return user


async def _tenant(session) -> Tenant:
    tenant = Tenant(clinic_name=f"Clínica {uuid4().hex[:6]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def test_the_resolver_reads_the_users_current_row(db_session):
    tenant = await _tenant(db_session)
    doctor = await _user(db_session, tenant.id)
    secretary = await _user(db_session, tenant.id, role="secretary")

    assert await resolve_agenda_scope(db_session, tenant.id, str(doctor.id)) == AGENDA_SCOPE_OWN
    assert await resolve_agenda_scope(db_session, tenant.id, str(secretary.id)) == AGENDA_SCOPE_CLINIC

    doctor.is_manager = True  # promoted after the token was minted: live, not from the token
    await db_session.flush()
    assert await resolve_agenda_scope(db_session, tenant.id, str(doctor.id)) == AGENDA_SCOPE_CLINIC


@pytest.mark.parametrize("actor", [None, "", "owner-a", "not-a-uuid", 42, str(uuid4())])
async def test_an_unknown_or_malformed_actor_is_own(db_session, actor):
    tenant = await _tenant(db_session)
    assert await resolve_agenda_scope(db_session, tenant.id, actor) == AGENDA_SCOPE_OWN


async def test_a_user_of_another_clinic_is_own_even_when_owner(db_session):
    clinic = await _tenant(db_session)
    other = await _tenant(db_session)
    foreign_owner = await _user(db_session, other.id, is_owner=True, is_manager=True)

    assert await resolve_agenda_scope(db_session, clinic.id, str(foreign_owner.id)) == AGENDA_SCOPE_OWN
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_hub_agenda_scope.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_api.services.hub_scope'`.

- [ ] **Step 3: Implement**

Create `src/brain_api/services/hub_scope.py`:

```python
"""Who sees the whole clinic agenda in secretarIA's hub (owner decision 2026-10-09 A).

secretarIA ENFORCES the rule (its hub routes filter and refuse); brain-api, the identity
authority, DECIDES it - at every hub-token introspection, from the acting user's CURRENT
row (auth-jwt-multitenant: mutable role state is read at request time, never trusted
from a token). The hub token itself is unchanged: it names the acting user in `act`.

    "clinic" - sees every professional's appointments: the clinic's owner/manager
               ("gestor": role `manager`, or a `doctor` with `is_owner`/`is_manager`,
               or the LEGACY `tenant_owner` during its token window) and the
               receptionist (`secretary`, who runs every agenda by product decision).
    "own"    - only the appointments of the user's own professional: any other doctor,
               and every case this module cannot prove (unknown/malformed actor, a user
               of another clinic, a role it does not know). Fail closed.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.models import User
from brain_api.models.user import (
    ROLE_DOCTOR,
    ROLE_MANAGER,
    ROLE_SECRETARY,
    ROLE_TENANT_OWNER,
)

AGENDA_SCOPE_CLINIC = "clinic"
AGENDA_SCOPE_OWN = "own"

_CLINIC_WIDE_ROLES = (ROLE_SECRETARY, ROLE_MANAGER, ROLE_TENANT_OWNER)


def agenda_scope_for(role: str, *, is_owner: bool, is_manager: bool) -> str:
    """The rule, pure. Roles are the exact stored strings (models/user.py)."""
    if role in _CLINIC_WIDE_ROLES:
        return AGENDA_SCOPE_CLINIC
    if role == ROLE_DOCTOR and (is_owner or is_manager):
        return AGENDA_SCOPE_CLINIC
    return AGENDA_SCOPE_OWN


async def resolve_agenda_scope(session: AsyncSession, tenant_id: UUID, actor: object) -> str:
    """The scope of the hub session whose token names `actor` (the `act` claim) and
    acts for `tenant_id` (the `sub` claim). Never raises for a bad `actor`."""
    try:
        user_id = UUID(str(actor))
    except (ValueError, TypeError, AttributeError):
        return AGENDA_SCOPE_OWN
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != tenant_id:
        return AGENDA_SCOPE_OWN
    return agenda_scope_for(
        user.role or "", is_owner=bool(user.is_owner), is_manager=bool(user.is_manager)
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_hub_agenda_scope.py -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff format src/brain_api/services/hub_scope.py tests/test_hub_agenda_scope.py
uv run ruff check --fix src/brain_api/services/hub_scope.py tests/test_hub_agenda_scope.py
git add src/brain_api/services/hub_scope.py tests/test_hub_agenda_scope.py
git commit -m "feat(hub): agenda scope rule read live from the acting user (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 2: The introspection answers `agenda_scope`

**Files:**
- Modify: `src/brain_api/schemas/internal.py` (`HubTokenVerifyOut`)
- Modify: `src/brain_api/api/internal.py` (`verify_hub_token`; import)
- Test: `tests/test_hub_agenda_scope.py` (append)

**Interfaces:**
- Consumes: `resolve_agenda_scope`, `AGENDA_SCOPE_*` (Task 1); `create_hub_token(tenant_id=..., actor_user_id=..., professional_id=...)`.
- Produces: `HubTokenVerifyOut.agenda_scope: Literal["clinic", "own"] = "own"` on `POST /internal/secretaria/hub-token/verify`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hub_agenda_scope.py`:

```python
# --- the introspection endpoint ------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

import brain_api.api.internal as internal_api  # noqa: E402
from brain_api.core.security import create_hub_token  # noqa: E402
from tests.test_rbac import (  # noqa: E402
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    CLINIC_A,
    CLINIC_B,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    _bearer,
    _token,
)

VERIFY = "/internal/secretaria/hub-token/verify"
PAIR = {"X-Internal-Api-Key": "pair-key"}


@pytest.fixture
def pair_key(monkeypatch):
    fake_settings = SimpleNamespace(SECRETARIA_API_KEY="pair-key", SECRETARIA_API_KEY_PREVIOUS="")
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)


async def _clinics(client) -> tuple[str, dict[str, str]]:
    admin = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin))).json()["items"]
    ids = {t["clinic_name"]: t["id"] for t in tenants}
    entitled = await client.patch(
        f"/admin/tenants/{ids[CLINIC_A]}/entitlements",
        headers=_bearer(admin),
        json={"plan": "complete_clinic_combo", "status": "active"},
    )
    assert entitled.status_code == 200, entitled.text
    return admin, ids


async def _create_user(client, admin, tenant_id, *, role, is_manager=False) -> str:
    response = await client.post(
        "/admin/users",
        headers=_bearer(admin),
        json={
            "email": f"{uuid4().hex[:10]}@clinica.com",
            "name": "Pessoa",
            "password": "senha1234",
            "role": role,
            "tenant_id": tenant_id,
            "is_manager": is_manager,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _owner_a_id(client, admin) -> str:
    users = (await client.get("/admin/users?limit=100", headers=_bearer(admin))).json()["items"]
    return next(u["id"] for u in users if u["email"] == OWNER_A_EMAIL)


async def _promote_to_manager(user_id: str) -> None:
    """The same in-memory DB the `client` fixture serves (its get_session override)."""
    from sqlalchemy import update

    from brain_api.core.database import get_session
    from brain_api.main import app

    session_gen = app.dependency_overrides[get_session]()
    session = await session_gen.__anext__()
    await session.execute(update(User).where(User.id == UUID(user_id)).values(role="manager"))
    await session.commit()
    await session_gen.aclose()


async def _verify(client, tenant_id, actor, **extra) -> dict:
    token = create_hub_token(tenant_id=tenant_id, actor_user_id=actor, **extra)
    response = await client.post(VERIFY, headers=PAIR, json={"token": token})
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_owner_sees_the_whole_clinic(client, pair_key):
    admin, ids = await _clinics(client)

    body = await _verify(client, ids[CLINIC_A], await _owner_a_id(client, admin))

    assert body["active"] is True and body["agenda_scope"] == "clinic"


@pytest.mark.parametrize(
    ("role", "is_manager", "expected"),
    [
        ("doctor", False, "own"),
        ("doctor", True, "clinic"),
        ("manager", False, "clinic"),
        ("secretary", False, "clinic"),
    ],
)
async def test_each_role_gets_its_scope(client, pair_key, role, is_manager, expected):
    admin, ids = await _clinics(client)
    user_id = await _create_user(client, admin, ids[CLINIC_A], role=role, is_manager=is_manager)

    body = await _verify(client, ids[CLINIC_A], user_id)

    assert body["agenda_scope"] == expected


async def test_a_promotion_counts_on_the_next_introspection_of_the_same_token(client, pair_key):
    admin, ids = await _clinics(client)
    user_id = await _create_user(client, admin, ids[CLINIC_A], role="doctor")
    token = create_hub_token(tenant_id=ids[CLINIC_A], actor_user_id=user_id)
    before = (await client.post(VERIFY, headers=PAIR, json={"token": token})).json()

    await _promote_to_manager(user_id)  # no admin route edits a role: write the row
    after = (await client.post(VERIFY, headers=PAIR, json={"token": token})).json()

    assert (before["agenda_scope"], after["agenda_scope"]) == ("own", "clinic")


async def test_an_actor_of_another_clinic_or_unknown_is_own(client, pair_key):
    admin, ids = await _clinics(client)
    clinic_b_manager = await _create_user(client, admin, ids[CLINIC_B], role="manager")

    assert (await _verify(client, ids[CLINIC_A], clinic_b_manager))["agenda_scope"] == "own"
    assert (await _verify(client, ids[CLINIC_A], "owner-a"))["agenda_scope"] == "own"
    assert (await _verify(client, ids[CLINIC_A], str(uuid4())))["agenda_scope"] == "own"


async def test_a_refused_token_is_still_a_200_with_own(client, pair_key):
    owner_jwt = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)

    as_user_jwt = await client.post(VERIFY, headers=PAIR, json={"token": owner_jwt})
    garbage = await client.post(VERIFY, headers=PAIR, json={"token": "not-a-jwt"})

    for response in (as_user_jwt, garbage):
        assert response.status_code == 200
        assert response.json() == {
            "active": False,
            "tenant_id": None,
            "professional_id": None,
            "agenda_scope": "own",
        }


async def test_professional_id_passthrough_is_unchanged(client, pair_key):
    admin, ids = await _clinics(client)
    professional_id = str(uuid4())

    body = await _verify(
        client, ids[CLINIC_A], await _owner_a_id(client, admin), professional_id=professional_id
    )

    assert body["professional_id"] == professional_id and body["tenant_id"] == ids[CLINIC_A]
```

Also change the file's first import line `from uuid import uuid4` to `from uuid import UUID, uuid4` (used by `_promote_to_manager`; there is no admin route that edits a user's role, verified 2026-10-09 — `grep -n "@router.patch" src/brain_api/api/admin.py` lists only entitlements and demo requests).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_hub_agenda_scope.py -q`
Expected: FAIL — `KeyError: 'agenda_scope'`.

- [ ] **Step 3: Implement the schema**

In `src/brain_api/schemas/internal.py`, in `HubTokenVerifyOut`, after `professional_id: UUID | None = None`, add:

```python
    # Owner decision 2026-10-09 A (services/hub_scope.py): "clinic" = sees every
    # professional's agenda (owner/manager, secretary); "own" = only the acting user's
    # own professional (any other doctor, an unknown actor, a refused token). Read LIVE
    # from the acting user's row at every introspection, never from the token. Additive:
    # a secretarIA that predates it ignores the key.
    agenda_scope: Literal["clinic", "own"] = "own"
```

and append one sentence to the class docstring: `` `agenda_scope` (2026-10-09) tells secretarIA whose appointments the session may see; "own" whenever brain-api cannot prove more. ``

- [ ] **Step 4: Implement the endpoint**

In `src/brain_api/api/internal.py`, add `from brain_api.services.hub_scope import resolve_agenda_scope` to the imports, and in `verify_hub_token` replace:

```python
    ent = await resolve_entitlement(session, tenant_id)
    active = ent.status in ACTIVE_STATUSES and ent.products.secretaria
    if not active:
        logger.info("hub_token_refused", tenant_id=str(tenant_id), status=ent.status)
    return HubTokenVerifyOut(active=active, tenant_id=tenant_id, professional_id=professional_id)
```

with:

```python
    ent = await resolve_entitlement(session, tenant_id)
    active = ent.status in ACTIVE_STATUSES and ent.products.secretaria
    if not active:
        logger.info("hub_token_refused", tenant_id=str(tenant_id), status=ent.status)
    # Owner decision 2026-10-09 A: whose appointments this hub session may see, from the
    # acting user's CURRENT row (services/hub_scope.py) - fail closed to "own".
    agenda_scope = await resolve_agenda_scope(session, tenant_id, claims.get("act"))
    return HubTokenVerifyOut(
        active=active,
        tenant_id=tenant_id,
        professional_id=professional_id,
        agenda_scope=agenda_scope,
    )
```

Also add one paragraph to the endpoint docstring: `` `agenda_scope` (2026-10-09): "clinic" or "own", read live from the user named by the token's `act` claim; "own" for anything that cannot be proven. ``

- [ ] **Step 5: Run the hub tests (old ones included)**

Run: `uv run pytest tests/test_hub_agenda_scope.py tests/test_auth_hardening.py -q`
Expected: PASS (the existing introspection tests keep their assertions; they only read the keys they knew).

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format tests/test_hub_agenda_scope.py
uv run ruff check src/brain_api/schemas/internal.py src/brain_api/api/internal.py tests/test_hub_agenda_scope.py
git diff --stat
git add src/brain_api/schemas/internal.py src/brain_api/api/internal.py tests/test_hub_agenda_scope.py
git commit -m "feat(internal): hub-token introspection answers agenda_scope (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 3: Contract, checkpoint and full validation

**Files:**
- Modify: `CONTRACTS.md` (§12.2, step 3 of the hub-token flow, and the `/internal/secretaria/hub-token/verify` row)
- Create: `docs/CHECKPOINT_hub_token_agenda_scope.md`
- Modify: `CLAUDE.md` (one pointer line under `## Documentação`)

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces: the record the R7 integrator and the owner read; no code.

- [ ] **Step 1: Run the whole suite and lint**

```bash
make test 2>&1 | tail -5
make lint
```

Expected: all green (baseline: 0 failures since `415935a`). Any failure: rerun that test on a clean `origin/main` with the same command; if it fails there too it is pre-existing (name it in the checkpoint with that proof), otherwise fix it before continuing. `make lint` red on files this plan did not touch is pre-existing — record, do not fix.

- [ ] **Step 2: Update the contract**

In `CONTRACTS.md` §12.2, in step 3 ("Introspect"), replace ``brain-api answers `{"active": bool, "tenant_id": uuid|null}` `` with:

```markdown
brain-api answers `{"active": bool, "tenant_id": uuid|null, "professional_id": uuid|null, "agenda_scope": "clinic"|"own"}`. `agenda_scope` (2026-10-09, owner decision A) is read LIVE from the user named by the token's `act` claim (`services/hub_scope.py`): `"clinic"` for a secretary, a manager, a doctor with `is_owner`/`is_manager` (and the legacy `tenant_owner`); `"own"` for any other doctor and for anything that cannot be proven (unknown actor, user of another tenant, refused token). secretarIA enforces it on its agenda (only that professional's appointments for `"own"`).
```

and in the `/internal/*` table row of `/internal/secretaria/hub-token/verify`, append: ``Answer also carries `professional_id` and `agenda_scope` (see §12.2 step 3).``

- [ ] **Step 3: Write the checkpoint**

Create `docs/CHECKPOINT_hub_token_agenda_scope.md`:

```markdown
# CHECKPOINT — Hub token: quem vê a agenda inteira (`agenda_scope`) (TASK-044)

Plano: `docs/superpowers/plans/2026-10-09-hub-token-papel-profissional.md`. Spec: `secretarIA/docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md` §5.A.

## Estado

- Branch `task/TASK-044-hub-token-papel` (worktree `C:\TECH\BRAIN-worktrees\TASK-044\brain-api`), commits locais: [lista do `git log --oneline origin/main..HEAD`].
- Suíte: [totais do Passo 1]. **Não mesclado, não pushado, não deployado. Sem migração.**

## O que mudou

- `services/hub_scope.py::agenda_scope_for` (regra) e `resolve_agenda_scope` (lê o usuário do claim `act`, ao vivo).
- `POST /internal/secretaria/hub-token/verify` responde também `agenda_scope: "clinic" | "own"` (`schemas/internal.py::HubTokenVerifyOut`). Token, emissão, `active`, `tenant_id` e `professional_id` sem mudança.

## Regra

`secretary`, `manager`, `doctor` com `is_owner`/`is_manager`, legado `tenant_owner` → `"clinic"`. Qualquer outro (médico comum, legado `tenant_staff`, ator desconhecido ou de outra clínica, token recusado) → `"own"`.

## Deploy (quando o dono autorizar)

Migração da secretarIA (`d8e3a5c1f7b2`) → **este brain-api** → `secretaria_api` + `secretaria-worker` juntos → front (R5). Este deploy sozinho não muda nada visível (a secretarIA de hoje ignora o campo). Sintoma de ordem trocada (secretarIA nova antes deste): gestor que também é médico só vê as próprias consultas e não vê o seletor "Todos / Só os meus".

## Pendências (fora deste plano)

- `GET /doctor/appointments` e `GET /doctor/patients` (este repo → `/internal` da secretarIA) continuam listando a clínica inteira para qualquer papel de `require_doctor`; a regra da agenda vale para o hub da secretarIA. Decidir com o dono se essas listas também devem respeitar `agenda_scope`.
```

- [ ] **Step 4: Point to it**

In `CLAUDE.md`, under `## Documentação`, add as the last paragraph:

```markdown
`docs/CHECKPOINT_hub_token_agenda_scope.md` — TASK-044: a introspecção do hub token responde `agenda_scope` ("clinic" | "own", lido ao vivo do usuário) para a secretarIA aplicar quem vê a agenda inteira; local, não deployado; deploy ANTES da secretarIA R7.
```

- [ ] **Step 5: Commit**

```bash
git diff --stat
git add CONTRACTS.md docs/CHECKPOINT_hub_token_agenda_scope.md CLAUDE.md
git commit -m "docs(internal): agenda_scope contract and checkpoint (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Hand back**

Report to the Integrator/owner in plain language (`AI_WORKFLOW.md` "Como falar com o dono"): brain-api now tells secretarIA, every time, whether the person sees the whole clinic's agenda or only their own; nothing was deployed; it must go live before the secretarIA R7 build. Push, merge and deploy only on the owner's explicit request.

## Self-Review

- Spec §5.A coverage: the decision is made in brain-api (Task 1), live (Task 1 resolver + Task 2 promotion test), carried on the existing introspection (Task 2), fail closed (Tasks 1–2), additive (Task 2 refusal/passthrough tests), documented with deploy order (Task 3).
- Placeholders: the two bracketed values in the checkpoint are filled from Step 1's output by design; nothing else is left open.
- Types: `agenda_scope_for(role, *, is_owner, is_manager) -> str`, `resolve_agenda_scope(session, tenant_id, actor) -> str`, `HubTokenVerifyOut.agenda_scope: Literal["clinic","own"]` — the same names in every task and in the secretarIA R7 plan (Task 13 reads `agenda_scope` and `professional_id`).
