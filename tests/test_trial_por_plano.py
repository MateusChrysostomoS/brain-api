"""O trial é por PLANO, não global (`billing._trial_days_for`).

`STRIPE_TRIAL_PERIOD_DAYS` nunca foi cortesia comercial: é a janela em que se
espera a Meta aprovar a conexão do WhatsApp Coexistence. Enquanto foi lido como
constante global, um comprador de PreCheck — que não tem conexão nenhuma a ser
aprovada — ganhava 75 dias grátis E lia, na página de pagamento, uma explicação
sobre aprovação da Meta que não existe no produto dele.

Pior: `harden_charge` (§13.4), que encerra o trial na ativação, só é chamado do
onboarding do secretarIA. Para um plano sem secretarIA ele nunca dispara — o
trial ia até o fim, sempre.
"""

from brain_api.services import billing, catalog


def _sel(plan_id: str) -> billing.CheckoutSelection:
    return billing.CheckoutSelection(plan_id=plan_id, addon_ids=())


def test_precheck_nao_tem_trial():
    assert billing._trial_days_for(_sel(catalog.PLAN_PRECHECK_BASIC)) == 0
    assert billing._trial_days_for(_sel(catalog.PLAN_PRECHECK_ADVANCED)) == 0


def test_plano_desconhecido_nao_tem_trial():
    # Falha para o lado de COBRAR, nunca para o de dar meses grátis por engano.
    assert billing._trial_days_for(_sel("plano-que-nao-existe")) == 0


def test_plano_com_secretaria_mantem_o_trial(monkeypatch):
    monkeypatch.setattr(billing.get_settings(), "STRIPE_TRIAL_PERIOD_DAYS", 75)
    com_secretaria = [p for p in catalog.PLAN_IDS if (catalog.get_plan(p) or None)
                      and catalog.get_plan(p).secretaria]
    assert com_secretaria, "catálogo sem nenhum plano de secretarIA — teste inútil"
    for plano in com_secretaria:
        assert billing._trial_days_for(_sel(plano)) == 75


def test_sem_trial_nao_ha_texto_de_espera_na_pagina():
    """A página de pagamento do PreCheck não pode falar de aprovação da Meta."""
    data: dict[str, str] = {}
    billing._apply_setup_custom_text(data, _sel(catalog.PLAN_PRECHECK_BASIC))
    assert data == {}


def test_com_trial_o_texto_aparece(monkeypatch):
    monkeypatch.setattr(billing.get_settings(), "STRIPE_TRIAL_PERIOD_DAYS", 75)
    plano = next(p for p in catalog.PLAN_IDS
                 if catalog.get_plan(p) and catalog.get_plan(p).secretaria)
    data: dict[str, str] = {}
    billing._apply_setup_custom_text(data, _sel(plano))
    assert "custom_text[submit][message]" in data
    assert "Coexistence" in data["custom_text[submit][message]"]
