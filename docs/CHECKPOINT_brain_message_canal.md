# CHECKPOINT — Canal de entrega (whatsapp / brain_message) no modelo de dados

Status: **BUILT + testado + migração aplicada e verificada em Postgres real (2026-09-08)**,
UNCOMMITTED, não deployado.

Peça 1 do conjunto de ~10 prompts do Brain-Message
(`z_prompts/PROMPT_BRAIN_MESSAGE_CANAL_ENTITLEMENT.md`). Escopo desta peça: **só o modelo
de dados de canal no brain-api**. Não mexe em sessão de paciente, OTP, switchboard,
secretarIA, PreCheck ou frontends — todos em prompts próprios que dependem desta.

## Por que

Até aqui o canal não existia no banco: todo tenant era WhatsApp por omissão, porque não
havia outro. O Brain-Message (console de atendimento + portal do paciente) é um segundo
canal de entrega, e nada no modelo sabia responder "esta clínica atende por qual canal?".
Sem essa resposta, nenhum dos outros prompts do conjunto tem como saber se um tenant tem
Brain-Message habilitado — era o item mais urgente do plano em
`TECH/BRAIN/ARQUITETURA_TOPOLOGIA.html` ("O canal não existe no modelo de dados... bloqueia
a venda").

## O que entrou onde

| arquivo | mudança |
|---|---|
| `src/brain_api/models/tenant.py` | `whatsapp_enabled: bool` + `brain_message_enabled: bool`, ambos `server_default=text("false")`, `default=False` |
| `migrations/versions/0017_message_channels.py` | nova revisão (`0016_courtesy_coupons` -> `0017_message_channels`): duas `add_column` `NOT NULL DEFAULT false` + backfill |
| `src/brain_api/schemas/entitlement.py` | novo `ChannelsOut{whatsapp, brain_message}` + campo `channels` em `EntitlementOut` |
| `src/brain_api/services/entitlements.py` | `resolve_entitlement` monta `channels` a partir do **`Tenant`** (nos dois ramos: com e sem linha de `entitlements`) |
| `tests/test_message_channels.py` | 8 testes novos |
| `CONTRACTS.md` §3.1 | `channels` no exemplo de resposta, na tabela de campos e nas regras de resolução |

`services/catalog.py` / `PlanDef` **não foram tocados**, de propósito (ver decisão 1).

## Verificação (o que foi de fato provado, não o que deveria funcionar)

**Suíte completa** (`uv run pytest -q`, de `C:\TECH\BRAIN\brain-api`):
→ **542 passed**, 0 failed, em 603s. Não só o arquivo novo.

**Migração em Postgres real** (container descartável `postgres:16` na 55434 — a 5432 estava
ocupada por outra sessão; `DATABASE_URL` passada por variável de ambiente, o `.env` local
não foi tocado):

1. `alembic upgrade 0016_courtesy_coupons` → limpo.
2. Inseridos 2 tenants ANTES da migração: um com `connected_at` carimbado, um sem.
3. `alembic upgrade head` → limpo, `0017_message_channels (head)`.
4. Resultado do backfill:

```
  clinic_name  | whatsapp_enabled | brain_message_enabled
---------------+------------------+-----------------------
 Conectada     | t                | f
 Em onboarding | f                | f
```

5. DDL conferida: as duas colunas saem `boolean`, `is_nullable=NO`, `column_default=false`.
6. `alembic downgrade -1` remove as duas colunas; `upgrade head` de novo reaplica e
   rebackfilla (o backfill é por predicado, então é idempotente).
7. `alembic revision --autogenerate` de sondagem: **nenhuma** diferença em `tenants` —
   modelo e migração batem. (A única deriva que o autogenerate acusa é pré-existente e
   alheia: índice vs. unique constraint em `precheck_topup_credits`. O arquivo de sondagem
   foi apagado.)

**Lint**: `ruff check` e `ruff format --check` **limpos nos 5 arquivos tocados**.
`make lint` no repo inteiro já está vermelho no HEAD (15 erros + 30 arquivos que o
`ruff format` reescreveria, todos em arquivos que esta sessão não tocou) — `ruff format .`
NÃO foi rodado, para não misturar uma reformatação de repo inteiro com esta mudança.

## Decisões tomadas nesta sessão (e por quê)

O dono pediu explicitamente para não ser consultado: cada decisão abaixo é a opção
escolhida + o argumento que a sustenta. Todas são reversíveis; a 2 é a única que mudaria o
contrato já publicado se for revista.

### 1. Dois booleanos, não um enum de canal — e no `Tenant`, não no `PlanDef`

**Escolhido:** `tenants.whatsapp_enabled` + `tenants.brain_message_enabled`.

Canal é **operacional** (como a clínica fala com o paciente), plano é **comercial** (o que
ela comprou). Trocar de canal não é trocar de plano, então `PlanDef` fica intocado — pôr
canal lá reintroduziria exatamente o acoplamento que esta peça existe para evitar.

Dois booleanos e não um enum porque `Entitlement.precheck_enabled`/`secretaria_enabled` já
modelam "quais" como **conjunto**, não como escolha exclusiva, e o caso de uso real é
migração gradual: a clínica mantém o WhatsApp dos pacientes antigos e oferece o
Brain-Message aos novos. Um enum único forçaria um corte tudo-ou-nada que não existe. O
teste `test_channels_are_a_set_not_an_exclusive_choice` trava essa propriedade — uma
regressão para enum quebra nele, não em produção.

### 2. `channels` como objeto aninhado, não dois campos soltos em `EntitlementOut`

**Escolhido:** `"channels": {"whatsapp": bool, "brain_message": bool}`, espelhando
`"products": {"precheck": bool, "secretaria": bool}` que já está do lado.

O prompt deixou a escolha aberta. A simetria com `products` é o argumento: quem lê o
payload aprende UMA forma ("um conjunto de flags nomeadas") e a aplica duas vezes. Dois
campos soltos no topo (`whatsapp_enabled`, `brain_message_enabled`) misturariam nível de
aninhamento com `products` sem ganho nenhum. `EntitlementOut` é `extra="ignore"`, então o
campo a mais não quebra nenhum consumidor atual (skill `frozen-contract-migration`).

### 3. O backfill é `connected_at IS NOT NULL` — e o prompt apontava para uma coluna que não existe

**Escolhido:** `UPDATE tenants SET whatsapp_enabled = true WHERE connected_at IS NOT NULL`.

O prompt mandava marcar "todo tenant que já tem WABA configurada" e dizia que o bloco em
`tenant.py:76` diria qual coluna checar. **Não diz**: aquele bloco é o test-window da Meta
(`test_window_started_at` / `test_window_notified_at`) e **não existe coluna de WABA no
`Tenant`** — brain-api não guarda `waba_id` nem `phone_number_id` (eles vivem na secretarIA;
aqui só passam por `POST /doctor/onboarding/attempts`).

O sinal equivalente que existe neste repo é `tenants.connected_at`, carimbado por
`services/onboarding.py::record_attempt` exatamente quando um attempt de conexão sai
`pass` — ou seja, "esta clínica conectou um número de verdade". É o mesmo fato, pela única
coluna que o representa aqui.

**As alternativas e por que foram recusadas:**

- *Marcar todo mundo* ("hoje todo tenant é WhatsApp na prática"): marcaria também clínicas
  paradas em `pending`/`aquecimento`/`aguardando_acao_manual`, que hoje não atendem por
  canal nenhum. Isso inventa um estado que elas não têm, e o primeiro prompt que ler
  `whatsapp_enabled` para decidir roteamento passaria a mandar mensagem para um canal
  inexistente. Fail-open num campo de roteamento é a pior direção de erro possível.
- *Usar `entitlements.secretaria_enabled`*: responde "comprou secretarIA", não "atende por
  WhatsApp" — é justamente a confusão plano-vs-canal que a decisão 1 evita.

**Consequência assumida:** um tenant pago que ainda não conectou número sai `false`/`false`.
Isso é a verdade sobre ele, não uma regressão — nenhuma leitura existente muda, porque hoje
nada lê estes campos. Quem liga o canal daqui para frente é o fluxo que o provisiona.

### 4. `InternalEntitlementOut` ficou de fora

`GET /internal/tenants/{id}/entitlements` (o resumo que o plugin da secretarIA consome) NÃO
ganhou `channels`. O prompt limita o escopo a `EntitlementOut` e proíbe mexer na secretarIA;
adicionar campo ali é a peça do
`PROMPT_BRAIN_MESSAGE_SECRETARIA_PIPELINE_CANAL.md`, que sabe qual é a strictness do parser
do outro lado. **Pendência explícita**, não esquecimento: quem executar aquele prompt vai
precisar deste campo no contrato interno.

## Pendências

- [ ] Commit + deploy (migração `0017` precisa rodar em produção).
- [ ] `InternalEntitlementOut` ganhar `channels` (prompt da secretarIA — ver decisão 4).
- [ ] Nada liga `brain_message_enabled` ainda: não há endpoint nem tela de admin que
      alterne canal. É de propósito — a UI é escopo dos prompts de frontend, e o
      provisionamento, do prompt do switchboard.
