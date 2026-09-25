# CHECKPOINT: clínica de teste criada pelo admin, sem Stripe (`POST /admin/tenants`)

Prompt: `z_prompts/PROMPT_BRAIN_ADMIN_TEST_TENANT_1_BRAIN_API.md` (parte 1/2). A parte 2 é
`..._2_BRAIN_FRONTEND.md`, no `brain-frontend`, e consome o contrato abaixo.

> **TEMPORÁRIO — REMOVER ANTES DO LANÇAMENTO REAL** (pedido do dono, 2026-09-25). Este
> endpoint é uma porta de teste: cria clínica ativa sem pagamento. Em produção de verdade não
> pode sobrar nenhuma forma de ter produto sem pagar. Checklist de remoção no fim deste doc.

**Estado (2026-09-25): BUILT, não commitado, não deployado. Migração nova `0025_tenant_is_test`
(aditiva) — NÃO aplicada em lugar nenhum; provada só via `create_all` em SQLite na suíte.** O código está na
mesma árvore do trabalho da auditoria de pontes, que também está sem commit
(`CHECKPOINT_provisioning_bridge_coverage.md`). O endpoint depende do helper
`ensure_products_provisioned` que aquele trabalho criou, então os dois precisam ir juntos
para o commit e para o deploy.

## Contrato HTTP

`POST /admin/tenants`. O acesso é só com JWT `admin`, pelo gate `require_role("admin")` que
vale para todo o router.

Corpo (`schemas/admin.py::AdminTenantCreateIn`, com `extra="forbid"`: campo desconhecido dá 422):

```json
{
  "clinic_name": "Clínica Teste",
  "email": "gestor@exemplo.com",
  "name": "Gestor Teste",
  "password": "senha1234",
  "precheck": false,
  "secretaria": true
}
```

| Campo | Regra |
|---|---|
| `clinic_name` | 1–255 caracteres, sem espaços nas pontas, não pode ser só espaço |
| `email` | `EmailStr`, até 320 caracteres, gravado em minúsculas |
| `name` | 1–255 caracteres, sem espaços nas pontas, não pode ser só espaço |
| `password` | 8–72 caracteres, com pelo menos uma letra e um dígito. É a mesma regra de `POST /admin/users`, implementada na mesma função (`_check_password_composition`). Nunca é ecoada nem logada |
| `precheck`, `secretaria` | `bool`, padrão `false`. Qualquer combinação vale, inclusive nenhum produto |

Resposta `201` (`AdminTenantCreateOut`):

```json
{
  "tenant_id": "uuid",
  "clinic_name": "Clínica Teste",
  "is_test": true,
  "entitlements": {
    "tenant_id": "uuid", "precheck_enabled": false, "secretaria_enabled": true,
    "plan": "secretaria_basico", "status": "active",
    "addons": {"...": false}, "limits": {"...": 0}, "usage": {},
    "period_start": null, "period_end": null,
    "stripe_customer_id": null, "stripe_subscription_id": null,
    "updated_at": "..."
  },
  "owner": {
    "id": "uuid", "tenant_id": "uuid", "clinic_name": "Clínica Teste",
    "email": "gestor@exemplo.com", "name": "Gestor Teste", "role": "manager",
    "is_manager": true, "is_owner": true, "created_at": "..."
  }
}
```

`entitlements` tem o mesmo formato de `EntitlementAdminOut`, que é o que
`GET /admin/tenants/{id}/entitlements` devolve. `owner` tem o mesmo formato de `AdminUserOut`,
o de `GET /admin/users`. Para navegar até o tenant criado, o frontend usa `tenant_id` em
`GET /admin/tenants/{tenant_id}`.

`is_test` (bool) também aparece em cada item de `GET /admin/tenants` e em
`GET /admin/tenants/{id}` (`false` para toda clínica real), para o frontend mostrar um selo
"teste".

**Efeito em outras rotas.** Para uma clínica com `is_test=true`, as quatro rotas que falam com
o Stripe respondem `403 {"detail": "test_tenant_billing_disabled"}` antes de qualquer
chamada ao Stripe: `POST /billing/checkout`, `POST /billing/portal`,
`POST /billing/precheck/topup` e `POST /billing/precheck/upgrade`.
`GET /billing/precheck/usage` continua funcionando, porque só lê.

Erros:

| Status | Quando | `detail` |
|---|---|---|
| `401` / `403` | sem token, ou token que não é admin (gate do router) | padrão do gate |
| `409` | e-mail já cadastrado, sem diferenciar maiúsculas. Nada é criado | `"Email already registered"` |
| `422` | corpo inválido: senha fora da regra, e-mail inválido, nome em branco ou campo extra | lista padrão do Pydantic |

## O que o endpoint faz

A criação acontece numa transação só (`services/admin.py::create_tenant`):

1. Cria o `Tenant(clinic_name)`. `brain_message_enabled` fica no padrão (`true`, migração 0023).
2. Cria o `User` dono/gestor com `role="manager"`, `is_owner=True` e `is_manager=True`. O hash da
   senha é `core.security.hash_password`.
3. Cria o `Entitlement` com `status="active"` e as colunas derivadas de um plano real do
   catálogo (decisão 1).
4. Faz o `commit`. Se a constraint única de `users.email` falhar numa corrida, o resultado é o
   mesmo 409.
5. Monta a resposta e só então chama `onboarding_sync.ensure_products_provisioned`, que
   provisiona na secretarIA e/ou no PreCheck os produtos ligados. A chamada é fail-soft: se a
   secretarIA ou o PreCheck estiver fora do ar, a clínica nasce ativa assim mesmo e o carimbo
   `*_provisioned_at` fica NULL até o retry preguiçoso de `GET /doctor/onboarding` corrigir.
6. Loga `admin_tenant_created` com `actor_user_id`, `tenant_id`, `owner_user_id` e os dois
   flags de produto. E-mail, nome e senha não vão para o log.

**Por que o Stripe nunca mexe nesse tenant (garantia fechada em 2026-09-25).** O
`/code-review` achou uma brecha na primeira versão. O webhook
(`billing._entitlement_for_event`) procura primeiro `metadata.tenant_id` e
`client_reference_id`, e as rotas `/billing/*` só exigiam `require_tenant`. Assim, o dono da
clínica de teste (ou um admin em "Modo médico" nela) podia abrir um checkout, e o evento de
volta ligaria a clínica ao Stripe. O dono decidiu fechar a brecha com uma coluna de teste,
em duas camadas:

1. **Porta:** a migração `0025_tenant_is_test` cria `tenants.is_test` (NOT NULL, default
   `false`), e `create_tenant` grava `true`. `api/billing.py::require_billable_tenant`
   substitui `require_tenant` nas quatro rotas que falam com o Stripe e responde 403 para
   clínica de teste.
2. **Fechadura:** `billing._entitlement_for_event` chama `_is_test_tenant` nos dois caminhos
   (metadata e `customer`). Um evento que aponte para clínica de teste vira "não resolvido":
   é logado como `stripe_event_test_tenant_ignored`, marcado como processado e nada muda.
   Isso vale até para um evento assinado de verdade.

Os outros pontos que falam com o Stripe já saem antes para essa clínica: o encaminhamento de
meter em `services/usage.py` e o restart da janela de teste em `api/onboarding.py` exigem
`stripe_customer_id`, que ela nunca tem. O checkout do signup público cria um tenant novo e
não mexe no de teste. Os ids do Stripe ficam no `Entitlement`, não no `Tenant` como o prompt
dizia.

## Decisões tomadas sem consultar o dono

1. **O entitlement sai de `catalog.compute_entitlement_state`, com um plano real escolhido pelo
   conjunto de produtos, e não é montado à mão.** Montar à mão com `plan="free"` e os flags
   ligados deixaria `secretaria_tier = None`, porque o tier deriva do plano e vai para a
   secretarIA pela `/internal`. Isso criaria uma clínica de teste diferente de uma clínica
   pagante com os mesmos produtos. O mapeamento fica em `services/admin.py::_TEST_TENANT_PLANS`:

   | precheck | secretaria | plano |
   |---|---|---|
   | não | não | `free` |
   | sim | não | `precheck_advanced` (a maior cota, a mesma do combo) |
   | não | sim | `secretaria_basico` |
   | sim | sim | `complete_clinic_combo` (inclui os add-ons do combo) |

   Não foi preciso inventar plano nenhum: cada combinação já tem um plano no catálogo. Para
   trocar depois, o admin usa `PATCH /admin/tenants/{id}/entitlements`, que já existe.
2. **O dono é `manager`, não `doctor`.** O prompt pedia assim, embora o signup público crie o
   dono como `doctor`. Conferi os dois pontos que dependem do papel: a ponte do PreCheck acha o
   dono por `is_owner`, não pelo papel, e `POST /sso/precheck/token` só barra `secretary`.
3. **`create_user` não é chamado direto.** Ele dá commit sozinho e exige que o tenant já exista,
   o que quebraria a transação única. O reaproveitamento é do que ele faz por dentro:
   `hash_password`, a regra "manager implica `is_manager`", o mesmo 409 e a mesma regra de senha
   (extraída para `_check_password_composition`, que `AdminUserCreateIn` também passou a usar,
   sem mudar comportamento).
4. **Os campos de produto são planos (`precheck`, `secretaria`), não uma lista.** Seguem o texto
   do prompt, e `extra="forbid"` recusa campos que não existem, como `stripe_customer_id`.
5. **A resposta não informa o estado das pontes.** Se uma ponte falhar, o carimbo NULL aparece
   no detalhe do tenant, e o retry preguiçoso resolve. Informar isso na resposta exigiria reler
   o tenant depois do rollback da ponte do PreCheck, a mesma armadilha de `MissingGreenlet`
   descrita no CHECKPOINT das pontes.
6. **Os testes ficam em arquivo próprio**, `tests/test_admin_tenant_create.py`, e não em
   `test_rbac.py`, que já é grande. Eles reaproveitam o `client` e os helpers de lá.

## Provas

- `tests/test_admin_tenant_create.py`, 15 testes:
  - as quatro combinações de produto resultam no plano, nos flags e no `status="active"`
    esperados, com os ids do Stripe NULL;
  - cada ponte é chamada, e o carimbo gravado, só quando o produto correspondente foi escolhido;
  - `brain_message_enabled` fica `true`;
  - um sensor em `billing._stripe_post`/`_stripe_get` mostra que o Stripe não é chamado;
  - o cenário do pedido, de ponta a ponta: com só `secretaria`, o dono criado faz login e
    `GET /entitlements` devolve `products.secretaria=true`, `products.precheck=false`,
    `status="active"`, sem chamada ao Stripe;
  - 409 com e-mail duplicado (inclusive com maiúsculas diferentes), sem criar tenant nem chamar
    ponte;
  - sete casos de 422, incluindo campo extra;
  - 403 para um token de médico;
  - secretarIA fora do ar: a resposta continua 201, a clínica fica ativa e o carimbo fica NULL.
- Mais 8 testes para `is_test` (23 no arquivo):
  - a clínica criada tem `is_test=true` na resposta, no banco, na listagem e no detalhe;
  - as clínicas reais continuam com `false`;
  - as quatro rotas de billing devolvem 403 `test_tenant_billing_disabled` para o dono da
    clínica de teste, sem chamada ao Stripe;
  - controle: uma clínica real não recebe esse 403;
  - o webhook ignora a clínica de teste nos dois caminhos (metadata e `customer`).
  Com as duas travas trocadas por `if False:`, esses 6 testes falham. Com as travas no lugar,
  os 23 passam.
- Suíte completa (`uv run python -m pytest`): 2026-09-24 **887 passed**; 2026-09-25, depois do
  `is_test`, **895 passed, 2 skipped, 0 failed**, exit 0. `ruff check .` limpo. O ruff apontou um erro de
  ordem de import em `scripts/create_courtesy_coupon.py` que já estava no HEAD (`d130063`) e foi
  corrigido junto.

## Nota para a auditoria de pontes

`CHECKPOINT_provisioning_bridge_coverage.md`, na tabela de cobertura, já lista este endpoint
como coberto desde o nascimento. Ele não precisa entrar numa próxima varredura.

## Remover antes do lançamento real (pedido do dono, 2026-09-25)

A coluna de teste só serve enquanto não há clientes pagantes. Antes do lançamento, retire a
porta inteira para que ninguém consiga ter produto sem pagar:

1. `POST /admin/tenants`: a rota em `api/admin.py::create_tenant`, o
   `services/admin.py::create_tenant` com `_TEST_TENANT_PLANS`, e os schemas
   `AdminTenantCreateIn`/`AdminTenantCreateOut`.
2. Antes de apagar a coluna, apague ou converta cada tenant com `is_test=true`. Se sobrar
   algum, ele fica ativo e sem cobrança para sempre. Pela API: `DELETE /admin/tenants/{id}`.
3. A coluna `Tenant.is_test`, numa migração nova com `drop_column`, e o campo `is_test` em
   `AdminTenantOut`/`AdminTenantDetailOut`.
4. As duas travas, **somente depois** dos passos 1 a 3: `require_billable_tenant` (as rotas
   voltam a usar `require_tenant`) e `billing._is_test_tenant`. Removê-las antes da coluna
   reabre a brecha.
5. A parte 2 no `brain-frontend`: o formulário de criar clínica e o selo "teste".
6. `tests/test_admin_tenant_create.py` e a linha do `POST /admin/tenants` em
   `CHECKPOINT_provisioning_bridge_coverage.md`.

`PATCH /admin/tenants/{id}/entitlements` também ativa produto sem Stripe, e já existia antes
deste trabalho. Ele não faz parte desta porta, mas é a mesma classe de risco. Antes do
lançamento, decida se ele continua existindo.

