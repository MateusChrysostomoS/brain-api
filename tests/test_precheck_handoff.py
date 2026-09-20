"""Tests for `POST /internal/precheck-handoff` — the secretarIA -> brain-api -> PreCheck
patient handoff (CONTRACTS.md §12.3, one leg of 3). brain-api is the entitlement
AUTHORITY here (PreCheck is never asked to re-check it) and keeps NO new state: pure
pass-through orchestration.

Ground truth: api/internal.py, services/precheck_handoff.py, schemas/internal.py.
Mirrors the pair-key gate pattern from test_usage_events.py and the fake-httpx
upstream-mapping pattern from test_doctor_secretaria.py / test_privacy.py.
"""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

import brain_api.api.internal as internal_api
from brain_api.services import precheck_handoff as precheck_handoff_service
from tests.test_rbac import ADMIN_EMAIL, ADMIN_PASSWORD, CLINIC_A, CLINIC_B, _bearer, _token

ROUTE = "/internal/precheck-handoff"
PHONE = "5511988887777"


async def _tenant_ids(client, admin_token: str) -> dict[str, str]:
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()
    return {t["clinic_name"]: t["id"] for t in tenants["items"]}


def _set_pair_key(monkeypatch, key: str = "pair-key") -> None:
    fake_settings = SimpleNamespace(SECRETARIA_API_KEY=key, SECRETARIA_API_KEY_PREVIOUS="")
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)


async def _patch_entitlement(client, admin_token: str, tenant_id: str, **fields) -> None:
    resp = await client.patch(
        f"/admin/tenants/{tenant_id}/entitlements", headers=_bearer(admin_token), json=fields
    )
    assert resp.status_code == 200, resp.text


CONFIGURED = SimpleNamespace(
    PRECHECK_BASE_URL="http://precheck:8000",
    PRECHECK_INTERNAL_TOKEN="precheck-internal-token",
    PRECHECK_TIMEOUT_SECONDS=10.0,
)


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


def _install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: _FakeResponse | None = None,
    exc: Exception | None = None,
) -> dict:
    """Point the outbound leg at a configured mesh + a fake httpx that records the call."""
    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def post(self, path: str, headers=None, json=None) -> _FakeResponse:
            captured["path"] = path
            captured["headers"] = headers
            captured["json"] = json
            if exc is not None:
                raise exc
            assert response is not None
            return response

    monkeypatch.setattr(precheck_handoff_service, "get_settings", lambda: CONFIGURED)
    monkeypatch.setattr(precheck_handoff_service.httpx, "AsyncClient", _FakeClient)
    return captured


def _body(tenant_id: str, phone_number: str = PHONE) -> dict:
    return {"tenant_id": tenant_id, "phone_number": phone_number}


# --- Pair-key gate ---------------------------------------------------------------


async def test_handoff_key_unset_forbidden(client):
    """SECRETARIA_API_KEY is forced empty by conftest -> fail closed with 403."""
    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "whatever"}, json=_body(str(uuid4()))
    )
    assert resp.status_code == 403


async def test_handoff_wrong_key_unauthorized(client, monkeypatch):
    _set_pair_key(monkeypatch)
    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "not-the-key"}, json=_body(str(uuid4()))
    )
    assert resp.status_code == 401


async def test_handoff_no_key_unauthorized(client, monkeypatch):
    _set_pair_key(monkeypatch)
    resp = await client.post(ROUTE, json=_body(str(uuid4())))
    assert resp.status_code == 401


# --- Entitlement gate: 403 not entitled (before any upstream call) -----------------


async def test_handoff_no_entitlement_row_403(client, monkeypatch):
    """Clinic B has no entitlement row -> default resolution (inactive, precheck off)."""
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = (await _tenant_ids(client, admin_token))[CLINIC_B]

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_b_id)
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "precheck_not_entitled"


async def test_handoff_inactive_status_403(client, monkeypatch):
    """Clinic A is precheck_enabled=True but status flipped to canceled -> refused."""
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    await _patch_entitlement(client, admin_token, tenant_a_id, status="canceled")

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "precheck_not_entitled"


async def test_handoff_precheck_disabled_403(client, monkeypatch):
    """Clinic A stays active/trialing but precheck_enabled is turned off -> refused."""
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    await _patch_entitlement(client, admin_token, tenant_a_id, precheck_enabled=False)

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "precheck_not_entitled"


# --- Unconfigured mesh: must fail LOUD (503), never degrade -------------------------


async def test_handoff_unconfigured_mesh_503(client, monkeypatch):
    """Entitled tenant, but PRECHECK_BASE_URL/PRECHECK_INTERNAL_TOKEN are unset in the
    hermetic test env (conftest) -> 503, no upstream call attempted."""
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "precheck_handoff_not_configured"


# --- Success mapping ----------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["seeded", "already_active"])
async def test_handoff_success_mapping(client, monkeypatch, outcome: str):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    captured = _install_fake_httpx(
        monkeypatch, response=_FakeResponse(200, {"status": outcome})
    )

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": outcome}
    assert captured["path"] == "/internal/precheck-handoff"
    assert captured["headers"]["X-Internal-Token"] == "precheck-internal-token"
    assert captured["json"] == {"brain_tenant_id": tenant_a_id, "phone_number": PHONE}
    assert captured["client_kwargs"]["base_url"] == "http://precheck:8000"


# --- Upstream 404 / 409 mapping ------------------------------------------------------


async def test_handoff_upstream_404_no_clinic(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    _install_fake_httpx(monkeypatch, response=_FakeResponse(404, {"detail": "not found"}))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "no_clinic_for_tenant"


async def test_handoff_upstream_409_conflict(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    _install_fake_httpx(monkeypatch, response=_FakeResponse(409, {"detail": "conflict"}))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "conflicting_active_session"


async def test_handoff_upstream_503_passthrough(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    _install_fake_httpx(monkeypatch, response=_FakeResponse(503, {"detail": "degraded"}))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] != "degraded"  # generic detail, upstream body not surfaced


# --- Upstream 5xx / 422 / network error collapse to 502, no leak --------------------


async def test_handoff_upstream_500_surfaces_502_no_leak(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    leaky = "some internal precheck stack trace"
    _install_fake_httpx(monkeypatch, response=_FakeResponse(500, {"detail": leaky}))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 502, resp.text
    assert leaky not in resp.text


async def test_handoff_upstream_422_surfaces_502_no_leak(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    leaky = "phone_number: field required"
    _install_fake_httpx(monkeypatch, response=_FakeResponse(422, {"detail": leaky}))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 502, resp.text
    assert leaky not in resp.text


async def test_handoff_network_error_surfaces_502(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    _install_fake_httpx(monkeypatch, exc=httpx.ConnectError("boom"))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 502, resp.text


# --- Validation ----------------------------------------------------------------------


@pytest.mark.parametrize("phone", ["", "1234567", "not-digits!", "1" * 16, "12 34 56 78"])
async def test_handoff_bad_phone_422(client, monkeypatch, phone: str):
    _set_pair_key(monkeypatch)
    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(str(uuid4()), phone)
    )
    assert resp.status_code == 422, resp.text


async def test_handoff_bad_tenant_id_422(client, monkeypatch):
    _set_pair_key(monkeypatch)
    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"tenant_id": "not-a-uuid", "phone_number": PHONE},
    )
    assert resp.status_code == 422, resp.text


# --- FEAT 38: optional booking context (patient_name + booked_service) ----------------
# brain-api is the STRICT hop of the mesh (`PrecheckHandoffIn` is `extra="forbid"`), so
# these tests pin BOTH halves of that contract: the new field names are now KNOWN, and
# everything else still isn't. See the `frozen-contract-migration` skill.

NAME = "Maria Aparecida de Souza"
SERVICE = "Consulta odontologica adulto"


def _body_ctx(tenant_id: str, **ctx: object) -> dict:
    """Today's body plus whichever FEAT 38 context keys the caller names."""
    return {**_body(tenant_id), **ctx}


async def _entitled_tenant(client) -> str:
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    return (await _tenant_ids(client, admin_token))[CLINIC_A]


async def test_handoff_forwards_both_context_fields(client, monkeypatch):
    """Both fields present -> accepted by the strict schema AND forwarded upstream under
    PreCheck's own field names (`patient_name`/`booked_service`, its FEAT 37)."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)
    captured = _install_fake_httpx(
        monkeypatch, response=_FakeResponse(200, {"status": "seeded"})
    )

    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json=_body_ctx(tenant_a_id, patient_name=NAME, booked_service=SERVICE),
    )
    assert resp.status_code == 200, resp.text
    assert captured["json"] == {
        "brain_tenant_id": tenant_a_id,
        "phone_number": PHONE,
        "patient_name": NAME,
        "booked_service": SERVICE,
    }


@pytest.mark.parametrize(
    "sent,absent",
    [
        ({"patient_name": NAME}, "booked_service"),
        ({"booked_service": SERVICE}, "patient_name"),
    ],
)
async def test_handoff_forwards_only_the_field_supplied(client, monkeypatch, sent, absent):
    """One field set, the other omitted -> only the set one goes on the wire. The absent
    one's KEY is gone entirely, not sent as an explicit `null`."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)
    captured = _install_fake_httpx(
        monkeypatch, response=_FakeResponse(200, {"status": "seeded"})
    )

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body_ctx(tenant_a_id, **sent)
    )
    assert resp.status_code == 200, resp.text
    assert captured["json"] == {
        "brain_tenant_id": tenant_a_id,
        "phone_number": PHONE,
        **sent,
    }
    assert absent not in captured["json"]


async def test_handoff_legacy_two_field_body_unchanged(client, monkeypatch):
    """REGRESSION -- the body secretarIA sends TODAY (before FEAT 39) must still produce a
    byte-identical outbound payload: NEITHER new key appears at all. This is the property
    that makes deploying brain-api alone a no-op instead of a change."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)
    captured = _install_fake_httpx(
        monkeypatch, response=_FakeResponse(200, {"status": "already_active"})
    )

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body(tenant_a_id)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "already_active"}
    assert captured["json"] == {"brain_tenant_id": tenant_a_id, "phone_number": PHONE}


async def test_handoff_explicit_null_context_is_omitted(client, monkeypatch):
    """An explicit `null` from the caller means "no context" and must NOT be forwarded as
    a literal null -- `None` is the absence of a value, never a value to write upstream."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)
    captured = _install_fake_httpx(
        monkeypatch, response=_FakeResponse(200, {"status": "seeded"})
    )

    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json=_body_ctx(tenant_a_id, patient_name=None, booked_service=None),
    )
    assert resp.status_code == 200, resp.text
    assert captured["json"] == {"brain_tenant_id": tenant_a_id, "phone_number": PHONE}


@pytest.mark.parametrize("unknown", ["patient_nome", "patientName", "service", "nome"])
async def test_handoff_unknown_field_still_422(client, monkeypatch, unknown: str):
    """REGRESSION of `extra="forbid"` -- widening the KNOWN field set must not have relaxed
    the schema. A typo'd / near-miss name still fails the WHOLE request, which is exactly
    the strictness this hop exists to provide (a silently-dropped field would be worse)."""
    _set_pair_key(monkeypatch)
    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json={**_body(str(uuid4())), unknown: "x"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("field", ["patient_name", "booked_service"])
async def test_handoff_context_over_255_chars_422(client, monkeypatch, field: str):
    """Both cap at 255 -- the same cap PreCheck's own schema uses, so an over-long value is
    refused HERE with a truthful 422 instead of collapsing into an opaque 502 when
    PreCheck 422s it later."""
    _set_pair_key(monkeypatch)
    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json={**_body(str(uuid4())), field: "x" * 256},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("field", ["patient_name", "booked_service"])
async def test_handoff_context_at_255_chars_accepted(client, monkeypatch, field: str):
    """Boundary: exactly 255 is valid and forwarded verbatim (no truncation happens here)."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)
    captured = _install_fake_httpx(
        monkeypatch, response=_FakeResponse(200, {"status": "seeded"})
    )
    value = "x" * 255

    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json={**_body(tenant_a_id), field: value},
    )
    assert resp.status_code == 200, resp.text
    assert captured["json"][field] == value


async def test_context_never_reaches_a_log_line(client, monkeypatch):
    """PII (FEAT 38 brief, section 3): `patient_name` must appear in NO log call this path
    makes -- not the success line, not an upstream-error warning, not a network-error one
    -- and `booked_service` is held to the same bar (no operational reason to log it).

    structlog's `PrintLoggerFactory` doesn't route through stdlib logging, so `caplog` is
    blind to it (same note as test_onboarding_endpoints.py) -- record what the loggers are
    CALLED with instead, on both modules this request touches: the router and the outbound
    service."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)

    logged: list[tuple] = []

    def _record(*args: object, **kwargs: object) -> None:
        logged.append((args, kwargs))

    for module in (internal_api, precheck_handoff_service):
        for level in ("debug", "info", "warning", "error"):
            monkeypatch.setattr(module.logger, level, _record, raising=False)

    body = _body_ctx(tenant_a_id, patient_name=NAME, booked_service=SERVICE)
    headers = {"X-Internal-Api-Key": "pair-key"}

    # Every outcome that logs on this path, with both context fields populated.
    _install_fake_httpx(monkeypatch, response=_FakeResponse(200, {"status": "seeded"}))
    assert (await client.post(ROUTE, headers=headers, json=body)).status_code == 200
    _install_fake_httpx(monkeypatch, response=_FakeResponse(500, {"detail": "boom"}))
    assert (await client.post(ROUTE, headers=headers, json=body)).status_code == 502
    _install_fake_httpx(monkeypatch, response=_FakeResponse(404, {"detail": "nope"}))
    assert (await client.post(ROUTE, headers=headers, json=body)).status_code == 404
    _install_fake_httpx(monkeypatch, exc=httpx.ConnectError("down"))
    assert (await client.post(ROUTE, headers=headers, json=body)).status_code == 502

    # Non-vacuous: we really did capture this path's log calls before asserting absence.
    assert any("precheck_handoff_ok" in repr(call) for call in logged), logged
    assert NAME not in repr(logged)
    assert SERVICE not in repr(logged)


# --- TASK-003 §5.1: `external_id` (a Portal patient) — accepted, validated, and REFUSED ----
#
# A Portal patient has `wa_id IS NULL` by design, so `phone_number` can never name them. The
# body now takes `external_id` INSTEAD — exactly one of the two, never both — and the leg
# stops at a deliberate `501`, because PreCheck's own `/internal/precheck-handoff` requires a
# phone number (verified against its live `openapi.json`) and this task may not change that
# repo. These tests pin the refusal so it cannot quietly become a fabricated phone number or a
# fabricated patient message later; see `api/internal.py::precheck_handoff`.

PORTAL_REFUSAL = "precheck_portal_handoff_unsupported"


def _body_portal(tenant_id: str, external_id: str | None = None) -> dict:
    return {"tenant_id": tenant_id, "external_id": external_id or str(uuid4())}


async def test_handoff_external_id_is_501_and_calls_nothing_upstream(client, monkeypatch):
    """The stop point, and the thing that must stay true about it: NO upstream request.

    A 501 that had already poked PreCheck would mean the refusal is cosmetic.
    """
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)
    captured = _install_fake_httpx(monkeypatch, response=_FakeResponse(200, {"status": "seeded"}))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body_portal(tenant_a_id)
    )
    assert resp.status_code == 501, resp.text
    assert resp.json()["detail"] == PORTAL_REFUSAL
    assert "path" not in captured, "the refusal must happen before any PreCheck call"


async def test_handoff_external_id_refusal_is_permanent_not_503(client, monkeypatch):
    """501, never 503. secretarIA distinguishes a permanent refusal from a transient outage
    (`brain-mesh-permanent-vs-transient-refusal`); a 503 here would have it retry a handoff
    that can never succeed, forever. A 422 would be worse still — it reads as a broken
    contract and makes a healthy mesh look misdeployed."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)
    _install_fake_httpx(monkeypatch, response=_FakeResponse(200, {"status": "seeded"}))

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body_portal(tenant_a_id)
    )
    assert resp.status_code == 501


async def test_handoff_external_id_still_passes_the_entitlement_gate_first(client, monkeypatch):
    """A clinic without PreCheck gets the SAME 403 it always did, even on the new leg.

    403 is the more actionable of the two answers and must not be masked by the 501: "this
    clinic has no PreCheck" is something the caller can fix; "brain-api cannot do Portal
    handoffs" is not.
    """
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = (await _tenant_ids(client, admin_token))[CLINIC_B]

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json=_body_portal(tenant_b_id)
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "precheck_not_entitled"


async def test_handoff_external_id_forwards_no_context_either(client, monkeypatch):
    """Booking context alongside `external_id` validates but still reaches nothing."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)
    captured = _install_fake_httpx(monkeypatch, response=_FakeResponse(200, {"status": "seeded"}))

    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json={**_body_portal(tenant_a_id), "patient_name": NAME, "booked_service": SERVICE},
    )
    assert resp.status_code == 501
    assert "path" not in captured


# --- The XOR itself ----------------------------------------------------------------------
#
# THESE ASSERT ON THE DETAIL, NOT JUST THE STATUS, and the reason is specific: before this
# change `external_id` was an unknown field, so `extra="forbid"` already answered 422 to every
# body below — for a completely different reason. A status-only assertion could not tell
# "the XOR validator did its job" from "someone deleted the validator and `extra="forbid"`
# happens to refuse the same bodies".

XOR_MESSAGE = "exactly one of phone_number or external_id"


async def test_handoff_both_handles_422(client, monkeypatch):
    """Two handles describe two patients; there is no defensible rule for picking one."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)
    _install_fake_httpx(monkeypatch, response=_FakeResponse(200, {"status": "seeded"}))

    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"tenant_id": tenant_a_id, "phone_number": PHONE, "external_id": str(uuid4())},
    )
    assert resp.status_code == 422, resp.text
    assert XOR_MESSAGE in resp.text, "422 came from somewhere other than the XOR validator"


async def test_handoff_no_handle_at_all_422(client, monkeypatch):
    """`phone_number` stopped being a REQUIRED field; it must not have stopped being
    mandatory. A body with neither handle is the same 422 it was before this change —
    and the detail proves it is now the XOR validator saying so, not a `missing` field."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)

    resp = await client.post(
        ROUTE, headers={"X-Internal-Api-Key": "pair-key"}, json={"tenant_id": tenant_a_id}
    )
    assert resp.status_code == 422, resp.text
    assert XOR_MESSAGE in resp.text


@pytest.mark.parametrize(
    "body_extra",
    [
        {"phone_number": None},
        {"external_id": None},
        {"phone_number": None, "external_id": None},
    ],
)
async def test_handoff_explicit_nulls_do_not_smuggle_a_missing_handle(
    client, monkeypatch, body_extra: dict
):
    """An explicit `null` is an ABSENT handle, not a present one — same 422 as omitting it."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)

    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"tenant_id": tenant_a_id, **body_extra},
    )
    assert resp.status_code == 422, resp.text
    assert XOR_MESSAGE in resp.text


async def test_handoff_explicit_null_phone_with_external_id_is_accepted(client, monkeypatch):
    """`{"phone_number": null, "external_id": "..."}` is exactly one handle, so it validates
    and reaches the documented 501 — not a 422. A caller that spells absence explicitly must
    not be punished for it."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)

    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"tenant_id": tenant_a_id, "phone_number": None, "external_id": str(uuid4())},
    )
    assert resp.status_code == 501, resp.text


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "12345", "  "])
async def test_handoff_bad_external_id_422(client, monkeypatch, bad: str):
    """The handle IS a `MessagePatient.id`; a value that could never be one is a 422 here
    rather than a 404 three hops later.

    The `loc` assertion matters: `extra="forbid"` would also 422 this body if the field were
    never declared, so only an error POINTING AT `external_id` proves the UUID type is live."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)

    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"tenant_id": tenant_a_id, "external_id": bad},
    )
    assert resp.status_code == 422, resp.text
    locs = [err.get("loc", []) for err in resp.json()["detail"]]
    assert any("external_id" in loc for loc in locs), resp.text


async def test_handoff_phone_shape_still_enforced_alongside_external_id(client, monkeypatch):
    """Making `phone_number` optional must not have relaxed its SHAPE."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)

    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"tenant_id": tenant_a_id, "phone_number": "not-digits"},
    )
    assert resp.status_code == 422, resp.text


async def test_handoff_unknown_handle_name_still_422(client, monkeypatch):
    """`extra="forbid"` survives the widening: `session_ref` is PreCheck's name for the same
    handle and is NOT accepted here (`frozen-contract-migration`)."""
    _set_pair_key(monkeypatch)
    tenant_a_id = await _entitled_tenant(client)

    resp = await client.post(
        ROUTE,
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"tenant_id": tenant_a_id, "session_ref": str(uuid4())},
    )
    assert resp.status_code == 422, resp.text
