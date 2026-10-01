"""Patient booking contact for the internal Brain-Message service boundary."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from brain_api.api.portal.internal import patient_contact
from brain_api.models.patient_access import MessagePatient, MessagePatientAccount
from brain_api.schemas.portal.internal import PendingIdentityIn


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows

    async def get(self, model, key):
        return self.rows.get((model, key))


async def test_prefers_the_account_email():
    tenant_id, patient_id, account_id = uuid4(), uuid4(), uuid4()
    patient = SimpleNamespace(tenant_id=tenant_id, account_id=account_id, email="old@example.test")
    account = SimpleNamespace(email="account@example.test")
    session = _FakeSession(
        {(MessagePatient, patient_id): patient, (MessagePatientAccount, account_id): account}
    )
    out = await patient_contact(
        PendingIdentityIn(tenant_id=tenant_id, external_id=patient_id), session=session
    )
    assert out.email == "account@example.test"


@pytest.mark.parametrize("account", [None, SimpleNamespace(email=None), SimpleNamespace(email="")])
async def test_falls_back_when_linked_account_has_no_address(account):
    tenant_id, patient_id, account_id = uuid4(), uuid4(), uuid4()
    patient = SimpleNamespace(
        tenant_id=tenant_id, account_id=account_id, email="legacy@example.test"
    )
    session = _FakeSession(
        {(MessagePatient, patient_id): patient, (MessagePatientAccount, account_id): account}
    )
    out = await patient_contact(
        PendingIdentityIn(tenant_id=tenant_id, external_id=patient_id), session=session
    )
    assert out.email == "legacy@example.test"


@pytest.mark.parametrize("email", ["legacy@example.test", None, ""])
async def test_unlinked_patient_contact(email):
    tenant_id, patient_id = uuid4(), uuid4()
    patient = SimpleNamespace(tenant_id=tenant_id, account_id=None, email=email)
    out = await patient_contact(
        PendingIdentityIn(tenant_id=tenant_id, external_id=patient_id),
        session=_FakeSession({(MessagePatient, patient_id): patient}),
    )
    assert out.email == ("legacy@example.test" if email else None)


async def test_other_tenant_cannot_read_linked_account_email():
    patient_id, account_id = uuid4(), uuid4()
    patient = SimpleNamespace(tenant_id=uuid4(), account_id=account_id, email="legacy@example.test")
    account = SimpleNamespace(email="account@example.test")
    session = _FakeSession(
        {(MessagePatient, patient_id): patient, (MessagePatientAccount, account_id): account}
    )
    out = await patient_contact(
        PendingIdentityIn(tenant_id=uuid4(), external_id=patient_id), session=session
    )
    assert out.email is None


async def test_unknown_patient_returns_no_email():
    out = await patient_contact(
        PendingIdentityIn(tenant_id=uuid4(), external_id=uuid4()), session=_FakeSession({})
    )
    assert out.email is None
