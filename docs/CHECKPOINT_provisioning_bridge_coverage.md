# CHECKPOINT — cobertura das pontes de provisionamento (secretarIA / PreCheck)

Origem: `z_prompts/PROMPT_BRAIN_API_SECRETARIA_PROVISIONING_GAP.md` (2026-09-24). Estado:
**BUILT, não commitado, não deployado, sem migração.** Depende de `6e7d001` +
migração `0023_brain_message_default_on` (já em `main`) só para o Brain-Message abrir; esta
mudança em si é independente de ordem de deploy.

## O problema

Uma clínica só ganha a linha `tenants` na secretarIA (sem a qual todo usuário dela toma
`404 "No tenant resolved for this subscription"` no Brain-Message) quando alguém chama
`services/onboarding_sync.py::ensure_secretaria_provisioned`. O irmão do PreCheck é
`ensure_precheck_provisioned` (clínica + `precheck_account_links`, sem o qual o SSO dá 409).
Os dois são fail-soft e idempotentes (nunca levantam; no-op depois do carimbo
`tenant.secretaria_provisioned_at` / `precheck_provisioned_at`), mas só eram chamados no
checkout de signup e no self-heal de `GET /doctor/onboarding`. Todo outro caminho de
ativação deixava a clínica sem provisionamento até o dono abrir `/app/onboarding`.

## A regra

**Todo caminho que liga `precheck_enabled`/`secretaria_enabled` chama
`onboarding_sync.ensure_products_provisioned(session, tenant_id)` depois do commit que
ativa o entitlement.** Ele lê o entitlement atual e chama cada ponte cujo produto está
ligado; nunca levanta (try/except próprio, além dos das pontes). Não criar fila nem retry:
a falha deixa o carimbo NULL e o self-heal de `/doctor/onboarding` tenta de novo.

Auditoria para o próximo caminho novo — repita o grep, não confie nesta tabela de memória:

```
grep -rn "ensure_.*_provisioned" src/
grep -rn "secretaria_enabled = \|precheck_enabled = \|\.status = \|Entitlement(\|Tenant(" src/
```

## Tabela de cobertura (conferida no código em 2026-09-24)

| Caminho de entrada | Ativa produto? | Antes | Depois |
|---|---|---|---|
| Webhook `checkout.session.completed` `kind=signup_intent` (`services/billing.py::apply_stripe_event` → `_apply_signup_intent_checkout`) | sim | as duas pontes, gated por produto (PreCheck→secretarIA sem recarga — ver decisão 6) | `ensure_products_provisioned` |
| `GET /doctor/onboarding` (`api/onboarding.py`) | não (self-heal) | as duas (secretarIA sem gate, por teste existente) | inalterado |
| `POST /public/courtesy-redemptions` (`api/public_signup.py::redeem_courtesy_coupon`) | sim | **só PreCheck** | `ensure_products_provisioned` |
| `PATCH /admin/tenants/{id}/entitlements` (`services/admin.py::update_entitlement`) | sim (plan/status/flags) | **nenhuma** | `ensure_products_provisioned` |
| Webhook `customer.subscription.created/updated` (`apply_stripe_event`, troca de plano p.ex. só-PreCheck → com secretarIA) | sim | **nenhuma** | `ensure_products_provisioned` (depois do commit final) |
| `POST /public/signup-intents` (`services/signup.py::register_signup`, único criador de `Tenant`) | não — entitlement inerte (`inactive`, produtos OFF) | — | não chama (decisão abaixo) |
| `POST /billing/precheck/upgrade` (`services/billing.py::upgrade_precheck_plan`) | não — só troca entre tiers do PreCheck, que já estava ligado | — | não precisa; o `subscription.updated` seguinte cobre |
| `services/usage.py::record_usage` (upsert de `Entitlement`) | não — só contador | — | não precisa |
| `customer.subscription.deleted` / `invoice.*` | desliga ou só muda status | — | não precisa |
| `scripts/seed_dev.py` | dev local | — | fora do app, não precisa |
| `POST /admin/tenants` (`services/admin.py::create_tenant`, 2026-09-24, `docs/CHECKPOINT_admin_test_tenant.md`) | sim (produtos escolhidos pelo admin, `active`) | não existia | `ensure_products_provisioned` desde o nascimento |

## Decisões tomadas sem consulta

1. **`register_signup` não provisiona.** O tenant nasce com os dois produtos OFF, e o próprio
   webhook já restringe a chamada de propósito ("uma compra só de PreCheck não deve acionar o
   provisionamento da secretarIA de jeito nenhum"). Provisionar um tenant inerte contrariaria
   essa regra e criaria linha na secretarIA para quem nunca pagou nem ganhou nada. Toda
   ativação posterior passa por um caminho da tabela acima.
2. **Helper único em vez de duas chamadas condicionais por caminho.** Os três caminhos
   corrigidos chamam `ensure_products_provisioned`. Assim um produto novo com ponte nova se
   liga num lugar só. O caminho do signup no webhook também passou a usar o helper (decisão 6).
3. **Webhook `subscription.*` também corrigido**, fora da lista original do prompt. Ele pedia
   para "não tocar em billing.py", mas também mandava conferir em vez de presumir, e a
   conferência achou a lacuna. A chamada é no-op barato quando já carimbado (as duas
   pontes checam o carimbo antes de qualquer I/O).
4. **Gate = flag do produto, não `status`**, igual ao webhook de signup existente.
5. **Lint pré-existente corrigido**: 12 achados de `ruff check` já vermelhos no HEAD (imports,
   linhas longas, 2 imports sem uso em teste) — correções mecânicas, sem mudança de
   comportamento. `ruff format` segue com drift pré-existente e não foi aplicado aos arquivos
   antigos (evita diff de ruído).

6. **Achado do `/code-review` (corrigido):** a ponte do PreCheck faz `session.rollback()`
   quando falha, o que expira o `tenant` carregado; a ponte da secretarIA, logo depois,
   lia `tenant.secretaria_provisioned_at` expirado numa `AsyncSession` → `MissingGreenlet`,
   engolido — e a clínica com os dois produtos voltava ao 404. O helper agora recarrega o
   tenant (`populate_existing=True`) antes de cada ponte, e o caminho de signup do webhook
   (que tinha a mesma ordem precheck→secretarIA) passou a usar o helper também. A cortesia
   faz `session.refresh(intent)` antes de `ready_status`, pelo mesmo motivo.

## Provas

- `tests/test_provisioning_bridge_coverage.py` (11 testes): para cortesia, PATCH do admin e
  webhook `subscription.updated`, (a) a ponte roda e `secretaria_provisioned_at` sai
  carimbado sem nenhuma visita a `/app/onboarding`, incluindo a cortesia com
  `secretaria_basico` (o sintoma relatado); (b) com a secretarIA fora do ar (retorno `False`
  e exceção), o caminho principal responde 200, o entitlement fica ativo e o carimbo fica
  NULL; (c) produto desligado → ponte não é chamada; (d) redelivery já carimbado → nenhuma
  chamada nova.
- Rodados contra o código pré-correção: 5 falham (os caminhos corrigidos); com a correção,
  11/11 passam.
- `test_rollback_da_ponte_precheck_nao_impede_a_secretaria` (cupom combo, ponte do PreCheck
  fazendo rollback): falha sem a recarga do tenant, passa com ela.
