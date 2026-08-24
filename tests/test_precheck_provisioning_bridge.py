"""Tests de `onboarding_sync.ensure_precheck_provisioned` — o bridge que faz
"pagou -> clínica no ar no PreCheck" existir.

Antes dele, pagar por PreCheck ativava o entitlement e parava aí: nenhuma clínica era
criada e `precheck_account_links` (que `POST /sso/precheck/token` EXIGE) só era populado
por script manual. O comprador via "nossa equipe entrará em contato em até 24 horas".

Cobre o contrato do irmão `ensure_secretaria_provisioned`:
- no-op depois de carimbado (idempotência barata, sem I/O);
- não provisiona quem não tem direito ao PreCheck;
- caminho feliz: chama o PreCheck, grava o link e carimba;
- **fail-soft**: qualquer erro do PreCheck NÃO levanta e NÃO carimba (o retry preguiçoso
  do portal tem de conseguir tentar de novo) — se levantasse, quebraria o webhook do
  Stripe e a Stripe redeliveraria para sempre;
- o template vem da escolha da vitrine e cai em `clinica-geral` quando ausente;
- a senha mandada ao PreCheck é aleatória (o médico entra por SSO; o brain é a
  autoridade de identidade e só tem o hash da senha real).
"""

import pytest
from sqlalchemy import select

from brain_api.models import Entitlement, PrecheckAccountLink, SignupIntent, Tenant, User
from brain_api.services import onboarding_sync


async def _tenant_com_precheck(db_session, *, precheck=True, clinic_name="Clínica Teste"):
    tenant = Tenant(clinic_name=clinic_name)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(Entitlement(
        tenant_id=tenant.id,
        plan="precheck_basic" if precheck else "secretaria_basico",
        precheck_enabled=precheck,
        secretaria_enabled=not precheck,
    ))
    owner = User(
        tenant_id=tenant.id, name="Dra. Ana", email="ana@clinica.com",
        password_hash="x", role="doctor", is_owner=True,
    )
    db_session.add(owner)
    await db_session.flush()
    return tenant, owner


@pytest.fixture
def chamadas(monkeypatch):
    """Captura as chamadas ao PreCheck e deixa o teste escolher a resposta."""
    registro = {"calls": [], "resposta": {"created": True, "doctor_user_id": 42}}

    async def fake(tenant_id, **kw):
        registro["calls"].append({"tenant_id": tenant_id, **kw})
        r = registro["resposta"]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(
        onboarding_sync.precheck_provisioning, "provision_clinic", fake
    )
    return registro


# ── guardas baratas ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_op_quando_ja_provisionado(db_session, chamadas):
    from datetime import UTC, datetime

    tenant, _ = await _tenant_com_precheck(db_session)
    tenant.precheck_provisioned_at = datetime.now(UTC)
    await db_session.flush()

    await onboarding_sync.ensure_precheck_provisioned(db_session, tenant)
    assert chamadas["calls"] == []  # nem chega a olhar entitlement


@pytest.mark.asyncio
async def test_nao_provisiona_sem_direito_a_precheck(db_session, chamadas):
    tenant, _ = await _tenant_com_precheck(db_session, precheck=False)

    await onboarding_sync.ensure_precheck_provisioned(db_session, tenant)
    assert chamadas["calls"] == []
    assert tenant.precheck_provisioned_at is None


# ── caminho feliz ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_provisiona_grava_link_e_carimba(db_session, chamadas):
    tenant, owner = await _tenant_com_precheck(db_session)
    db_session.add(SignupIntent(
        tenant_id=tenant.id, name="Ana", clinic_name=tenant.clinic_name,
        email=owner.email, whatsapp_phone="5521999999999",
        catalog_ids=["precheck_basic"], precheck_template_slug="cardiologia",
    ))
    await db_session.flush()

    await onboarding_sync.ensure_precheck_provisioned(db_session, tenant)

    assert len(chamadas["calls"]) == 1
    call = chamadas["calls"][0]
    assert call["template_slug"] == "cardiologia"      # escolha da vitrine respeitada
    assert call["doctor_email"] == owner.email
    assert call["doctor_phone"] == "5521999999999"     # vem do intent, não do tenant
    assert len(call["doctor_password"]) >= 32          # aleatória: o médico entra por SSO

    # o link é o ponto do bridge: sem ele o SSO responde 409
    link = await db_session.scalar(
        select(PrecheckAccountLink).where(PrecheckAccountLink.brain_user_id == owner.id)
    )
    assert link is not None
    assert link.precheck_user_id == 42
    assert tenant.precheck_provisioned_at is not None


@pytest.mark.asyncio
async def test_sem_intent_cai_no_template_padrao(db_session, chamadas):
    tenant, _ = await _tenant_com_precheck(db_session)

    await onboarding_sync.ensure_precheck_provisioned(db_session, tenant)

    assert chamadas["calls"][0]["template_slug"] == onboarding_sync.DEFAULT_PRECHECK_TEMPLATE
    assert tenant.precheck_provisioned_at is not None


@pytest.mark.asyncio
async def test_slug_e_trigger_sao_deterministicos(db_session):
    tenant, _ = await _tenant_com_precheck(db_session, clinic_name="Clínica São José")
    a = onboarding_sync._precheck_identity(tenant.clinic_name, tenant.id)
    b = onboarding_sync._precheck_identity(tenant.clinic_name, tenant.id)
    assert a == b                                    # retentativa gera o mesmo
    slug, trigger = a
    assert slug.startswith("clinica-sao-jose")       # acento normalizado
    assert " " in trigger and str(tenant.id)[:8] in slug


# ── fail-soft (o que protege o webhook do Stripe) ───────────────────────────


@pytest.mark.asyncio
async def test_falha_do_precheck_nao_levanta_e_nao_carimba(db_session, chamadas):
    tenant, _ = await _tenant_com_precheck(db_session)
    chamadas["resposta"] = None  # cliente devolve None em qualquer falha

    await onboarding_sync.ensure_precheck_provisioned(db_session, tenant)  # não levanta

    # sem carimbo, o retry preguiçoso do portal tenta de novo
    assert tenant.precheck_provisioned_at is None


@pytest.mark.asyncio
async def test_excecao_inesperada_e_engolida(db_session, chamadas):
    tenant, _ = await _tenant_com_precheck(db_session)
    chamadas["resposta"] = RuntimeError("boom")

    await onboarding_sync.ensure_precheck_provisioned(db_session, tenant)  # não levanta

    assert tenant.precheck_provisioned_at is None


@pytest.mark.asyncio
async def test_resposta_sem_doctor_user_id_nao_carimba(db_session, chamadas):
    tenant, _ = await _tenant_com_precheck(db_session)
    chamadas["resposta"] = {"created": True}  # sem o id: SSO seria impossível

    await onboarding_sync.ensure_precheck_provisioned(db_session, tenant)

    assert tenant.precheck_provisioned_at is None
