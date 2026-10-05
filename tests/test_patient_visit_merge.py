"""Completing a visit discards its chat only when the clinic already knows the account."""

from tests.test_patient_access import (
    PATIENT_EMAIL,
    _bearer,
    _configure_mesh,
    _peek_code,
    _seed_identity,
    _spy_transport,
)
from tests.test_patient_pending_session import _claim, _identity_call, _open


async def _verified_visit(client, sessionmaker, seed, monkeypatch):
    from brain_api.services import secretaria_provisioning

    async def queued(to, template, variables):
        return True

    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", queued)
    opened = await _open(client, sessionmaker, seed.both)
    assert opened.status_code == 200
    body = opened.json()
    handle = body["patient_ref"]
    assert (await _claim(client, monkeypatch, seed.both, handle)).status_code == 200
    asked = await _identity_call(client, monkeypatch, "pending-otp/request", seed.both, handle)
    assert asked.status_code == 200
    code = await _peek_code(sessionmaker, None, PATIENT_EMAIL)
    verified = await _identity_call(
        client, monkeypatch, "pending-otp/verify", seed.both, handle, code=code
    )
    assert verified.status_code == 200
    return handle, _bearer(body["pending_token"])


async def _visit_with_twin(client, sessionmaker, seed, monkeypatch):
    existing = await _seed_identity(sessionmaker, seed.both, PATIENT_EMAIL)
    handle, headers = await _verified_visit(client, sessionmaker, seed, monkeypatch)
    return handle, str(existing), headers


async def _verified_visit_without_twin(client, sessionmaker, seed, monkeypatch):
    _, headers = await _verified_visit(client, sessionmaker, seed, monkeypatch)
    return headers


async def test_completing_a_superseded_visit_tells_secretaria_to_merge(pclient, monkeypatch):
    """A twin identity exists at the clinic => the visit's chat is discarded upstream."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})
    # A fresh visit proves the address of an existing identity at this clinic.
    visit_ref, old_ref, headers = await _visit_with_twin(client, sessionmaker, seed, monkeypatch)

    resp = await client.post("/patient-access/pending/complete", headers=headers)
    assert resp.status_code == 200

    merges = [c for c in calls if c["url"] == "/internal/brain-message/visits/merge"]
    assert len(merges) == 1
    assert merges[0]["json"] == {
        "tenant_id": str(seed.both),
        "visit_external_id": visit_ref,
        "into_external_id": old_ref,
    }


async def test_completing_a_first_time_visit_does_not_merge_anything(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})
    headers = await _verified_visit_without_twin(client, sessionmaker, seed, monkeypatch)

    resp = await client.post("/patient-access/pending/complete", headers=headers)
    assert resp.status_code == 200
    assert [c for c in calls if c["url"].endswith("/visits/merge")] == []


async def test_a_secretaria_that_refuses_the_merge_never_breaks_the_login(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    _spy_transport(monkeypatch, status_code=404, payload={"detail": "nope"})
    _, _, headers = await _visit_with_twin(client, sessionmaker, seed, monkeypatch)
    resp = await client.post("/patient-access/pending/complete", headers=headers)
    assert resp.status_code == 200


async def test_merge_is_fail_soft_when_transport_raises(monkeypatch):
    from uuid import uuid4

    import httpx

    from brain_api.services.message_switchboard import merge_visit

    _configure_mesh(monkeypatch)

    async def broken(*args, **kwargs):
        raise httpx.InvalidURL("invalid synthetic URL")

    monkeypatch.setattr(httpx.AsyncClient, "request", broken)
    assert await merge_visit(tenant_id=uuid4(), visit_ref="visit", into_ref="account") == "failed"


async def test_merge_is_fail_soft_when_unconfigured(monkeypatch):
    from uuid import uuid4

    from brain_api.config import get_settings
    from brain_api.services.message_switchboard import merge_visit

    monkeypatch.setattr(get_settings(), "SECRETARIA_BASE_URL", "")
    assert (
        await merge_visit(tenant_id=uuid4(), visit_ref="visit", into_ref="account")
        == "unconfigured"
    )
