# CHECKPOINT — capacidade de profissionais pelo admin (TASK-063)

Estado: **implementado e commitado só na branch `task/TASK-063-capacidade-profissionais`
(worktree `BRAIN-worktrees/TASK-063/brain-api`). Não mesclado em `main`, não pushado, não
deployado. Sem migração.**

## Por que existe

O dono precisa montar clínicas de teste com vários médicos (aceite multi-clínica, TASK-057 a
061). A secretarIA recusa o convite de um profissional quando a clínica não tem o add-on
`multi_professional` (403 `multi_professional_not_entitled`) ou quando já chegou em
`limits["professionals"]` (409 `professional_limit_reached`) —
`secretarIA/src/secretaria/services/professional_capacity.py::require_professional_capacity`.
Clínica de teste nasce com o limite do plano (1). Não havia como mudar isso pela tela.

## Contrato HTTP

Ambas atrás do `require_role("admin")` do router `/admin` (`api/admin.py`).

`GET /admin/tenants/{tenant_id}/professional-capacity` → 200 `ProfessionalCapacityOut`

`PUT /admin/tenants/{tenant_id}/professional-capacity` com corpo fechado (`extra="forbid"`):

```json
{ "max_professionals": 3 }
```

Resposta (as duas rotas):

```json
{
  "tenant_id": "…",
  "max_professionals": 3,
  "multi_professional": true,
  "active_professionals": 1,
  "editable": true,
  "blocked_reason": null,
  "max_allowed": 50
}
```

- `active_professionals`: contagem ao vivo da equipe ativa na secretarIA
  (`secretaria_provisioning.get_professionals`, inativos não contam). `null` = não deu para ler
  (secretarIA fora/desconfigurada, ou produto desligado) — nunca significa zero.
- `blocked_reason`: `stripe_managed` (há `stripe_subscription_id`: o próximo webhook desfaria a
  edição) ou `secretaria_disabled` (o limite só existe para a secretarIA). `editable` é o
  inverso.

Erros do PUT: 404 clínica inexistente; 409 `stripe_managed` | `secretaria_disabled` |
`below_active_professionals`; 422 fora de `1..50`, não inteiro, ou qualquer campo extra
(inclusive `tenant_id` e `addons` — o add-on nunca vem do cliente).

## O que o PUT grava

`services/professional_capacity.py::set_capacity`, na linha de `entitlements`:

- `addons.multi_professional = true` (sem ele o convite é recusado com qualquer limite);
- `limits.professionals = N`, mesclado sobre o cálculo do catálogo e sobre os outros limites
  já gravados (os demais não mudam).

É o mesmo número que a secretarIA recebe: `GET /internal/tenants/{id}/entitlements` usa
`services/entitlements.py::resolve_entitlement`, que mescla `limits` gravado sobre o catálogo
(teste `test_definir_capacidade_liga_addon_e_grava_o_limite_que_a_secretaria_le`). A secretarIA
lê fresco (`redis=None`) no caminho de convite/ativação, então vale no próximo convite, sem
deploy nem cache.

Idempotente. Nunca desativa ninguém.

## Decisões tomadas sem consulta

1. **Rota dedicada em vez de reaproveitar `PATCH …/entitlements`.** O PATCH aceita `addons` e
   `limits` livres; para esta tela isso deixaria o cliente escolher o add-on e qualquer chave.
   A rota nova aceita um único inteiro e deriva o resto.
2. **Teto de 50.** Um erro de digitação (1000) é recusado. Constante
   `schemas/admin.py::ADMIN_MAX_PROFESSIONALS`.
3. **Recusa baixar abaixo da equipe ativa** (409 `below_active_professionals`). Baixar não
   desativaria ninguém, mas deixaria a clínica num estado em que todo convite falha sem
   explicação. Se a equipe não pôde ser lida, não bloqueia (a secretarIA reconfere em cada
   adição).
4. **Recusa clínica com assinatura Stripe.** O Stripe é dono da linha
   (`docs/CHECKPOINT_billing_add_product.md`); a edição duraria até o próximo evento.
5. **Sem transação aberta durante a chamada à secretarIA.** A leitura da equipe é rede: a
   sessão é fechada antes (`session.close()`) e a linha é relida com `FOR UPDATE` para gravar.
   Motivo: conexão "idle in transaction" já travou migração em produção neste workspace.
6. **Correção de passagem no PATCH existente** (`services/admin.py::update_entitlement`, ramo
   `addons`): antes, qualquer PATCH com `addons` recalculava `limits` do catálogo e apagava o
   limite definido aqui. Agora, enquanto `multi_professional` continua ligado (e o mesmo pedido
   não troca o `plan`), o limite gravado é mantido. Desligar o add-on ou trocar o plano segue
   voltando ao padrão do plano — de propósito.

## Riscos aceitos

- **Corrida pequena:** a equipe é contada antes do lock. Um profissional ativado na secretarIA
  nesse intervalo pode deixar o limite um abaixo da equipe. Ninguém é desativado; o próximo
  convite é recusado. O `FOR UPDATE` não é exercitado pela suíte (SQLite ignora).
- **Assinatura posterior apaga o limite:** se uma clínica manual assinar pelo Stripe depois, o
  webhook reescreve `addons`/`limits` a partir da assinatura (`services/billing.py`). Coerente
  com "o Stripe é dono da linha"; a tela avisa.

## Provas

- `tests/test_admin_professional_capacity.py`: 19 testes — leitura, gravação (incluindo o que
  `resolve_entitlement` devolve), idempotência, recusa abaixo da equipe ativa sem gravar,
  equipe ilegível, clínica sem secretarIA (sem chamar a secretarIA), clínica com Stripe, PATCH
  de produto e de outro add-on preservando o limite, desligar o add-on voltando ao plano, linha
  antiga sem a chave, 6 corpos inválidos, 404, 401/403 para não-admin.
- Revisão independente (`ecc:code-reviewer`): sem bloqueante; os 2 médios e 3 baixos foram
  tratados (itens 6 e "Riscos aceitos" acima, `_current_cap` com a mesma mescla do
  `resolve_entitlement`).
- Suíte completa e lint: ver `BRAIN/tasks/TASK-063/TASK.md`.

## Deploy (quando autorizado)

Sem migração. Ordem: brain-api → brain-frontend (a tela chama as rotas novas; antes do
brain-api, o card mostra erro de carregamento e o resto da página segue funcionando).
