# CHECKPOINT — as três faixas do PreCheck (50 / 100 / 300)

> 2026-09-03. Origem: a tabela comercial que o Diogo escreveu (`PreCheck/docs/FEEDBACK
> DIOGO.docx`) pede **três** planos; o catálogo tinha **duas** faixas, e o
> `STRIPE_PRICE_MAP` de produção vendia o Basic por **R$ 1,00** (um Price de teste que
> ficou apontado depois de um teste de funil).
>
> **Estado: NO AR em 2026-09-03**, os três passos da §4 concluídos e provados em produção
> (§5.2). A ordem da §4 continua valendo para qualquer faixa futura.

## 1. A tabela

| faixa | catalog id | cota/mês | preço | Stripe Price (live) |
|---|---|---|---|---|
| Start | `precheck_start` | 50 | **R$ 119,99** | `price_1UBeeTDVppnulHzLRECfw7Pn` (prod `prod_VC2j2aQCU8CDSh`) |
| Basic | `precheck_basic` | 100 | **R$ 209,99** | `price_1U7xhtDVppnulHzLzZLYtp4F` |
| Advanced | `precheck_advanced` | 300 | **R$ 599,99** | `price_1U7xhuDVppnulHzLFdlP1aCf` |
| avulso | `precheck_topup` | — | R$ 2,50 **por pré-consulta** (mín. 5) | `price_1U6vByDVppnulHzLOWuGXeGz` |

**Por que o Start entrou EMBAIXO em vez de renomear as faixas.** As linhas de
`entitlements`, as chaves do price map e os tenants já provisionados soletram
`precheck_basic`/`precheck_advanced`; renomear pediria migração de dados para não comprar
nada. O `LEGACY_PLAN_ALIASES["precheck"]` continua resolvendo para **Basic** — reapontá-lo
para a faixa mais barata rebaixaria em silêncio a cota de toda linha e todo link antigo.

## 2. O que mudou no Stripe (conta live, feito em 2026-09-03)

- **Criado** o produto `PreCheck Start` + o Price recorrente mensal de R$ 119,99.
- **Arquivado** `price_1U87GVDVppnulHzL1tpwi8Ia` (**R$ 1,00/mês**, no produto PreCheck
  Basic): era o Price que o `STRIPE_PRICE_MAP` de produção apontava como `precheck_basic`.
  Arquivar impede compra nova; **não** mexe em assinatura existente.
- Os Prices de R$ 209,99 e R$ 599,99 **já existiam** e já eram o `default_price` dos seus
  produtos — só não estavam no price map.

⚠️ **Três assinaturas live seguem no Price de R$ 1,00** (`sub_1U88Ww…`, `sub_1U87vF…`,
`sub_1U87TQ…`, clientes `teste-funil@example.com` / `teste2@` / `teste3@`) — são testes de
funil do Lucas, não clientes. Arquivar o Price não as cancela: elas continuam cobrando
R$ 1,00/mês até serem canceladas no dashboard.

## 3. O que mudou no código

**brain-api**

| Arquivo | O quê |
|---|---|
| `config.py` | `PRECHECK_START_CONSULTATIONS_PER_MONTH: int = 50` (default já é o valor de venda → **nenhuma env var nova é obrigatória** no EasyPanel) |
| `services/catalog.py` | `PLAN_PRECHECK_START`, o `PlanDef` do Start e **`PRECHECK_TIER_PLAN_IDS`** — a escada de faixas num lugar só, da menor cota para a maior |
| `services/billing.py` | `upgrade_precheck_plan` valida o destino contra `PRECHECK_TIER_PLAN_IDS` em vez do par Basic/Advanced escrito à mão |
| `schemas/billing.py`, `api/billing.py` | docstring/summary do upgrade deixam de prometer duas faixas |
| `.env.example`, `CONTRACTS.md` §3.2 | a chave `precheck_start` no price map, a cota, e a tabela de planos (que ainda descrevia o `precheck` único de antes do split de 2026-08-01) |

Nada mais precisou mudar: signup público, checkout, PATCH de admin e o recompute do webhook
derivam tudo de `catalog.PLAN_IDS`/`ASSIGNABLE_PLAN_IDS`, então a faixa nova passa a existir
em todos eles pelo simples fato de estar no catálogo.

**`PRECHECK_TIER_PLAN_IDS` exclui o combo de propósito.** `complete_clinic_combo` é
`precheck=True` e carrega a cota do Advanced; se o teste de destino do upgrade fosse
"`plan.precheck`", um tenant do combo poderia trocar para um plano PreCheck-only e **perder
a secretarIA** em silêncio. Tem teste travando isso.

**brain-frontend**

| Arquivo | O quê |
|---|---|
| `app/(site)/_lib/pricing.ts` | `PRECHECK_QUOTA` ganha `start: 50`; card `precheckStart`; **os três `amount` viram os preços de venda** (eram R$ 59,99 / R$ 169,99) |
| `app/(site)/page.tsx` + `brand-ds.css` | as 3 faixas numa linha (`.price-grid--3`), combo + secretarIA na linha de baixo (`.price-grid--2`) — 5 cards num grid de 4 colunas deixariam um card órfão |
| `cadastro/lib/plans.ts` | `PURCHASABLE_PLANS` e a família `["precheck_start","precheck_basic","precheck_advanced"]` — o passo de escolha do wizard passa a mostrar três |
| `lib/manage-api.ts` | `precheck_start` em `CatalogPlanId`; novo `PrecheckTierPlanId` (o destino aceito pelo upgrade) |
| `app/(site)/app/billing/_components/PrecheckBillingSection.tsx` | o botão fixo "Fazer upgrade para Advanced" (só para `precheck_basic`) vira **um botão por faixa acima da atual**, com o alvo guardado no estado da confirmação |

## 4. Ordem de deploy — não inverter

`services/billing.py::_parse_price_map` **rejeita id desconhecido do catálogo** (de
propósito: um map com typo não pode desvender um produto em silêncio). Então:

1. **Deploy do brain-api COM este código** (é o que ensina o catálogo a conhecer
   `precheck_start`).
2. **Só depois** trocar o `STRIPE_PRICE_MAP` no EasyPanel:

```json
{"precheck_start":"price_1UBeeTDVppnulHzLRECfw7Pn","precheck_basic":"price_1U7xhtDVppnulHzLzZLYtp4F","precheck_advanced":"price_1U7xhuDVppnulHzLFdlP1aCf","precheck_topup":"price_1U6vByDVppnulHzLOWuGXeGz"}
```

   (mais as chaves `secretaria_*`/add-ons que já estiverem lá — este bloco cobre só a parte
   PreCheck.) Reiniciar o serviço: as cotas são lidas **uma vez**, no import do catálogo.
3. **Deploy do brain-frontend** (export estático: preço é string no bundle, trocar env no
   painel não faz nada).

Invertendo 1 e 2, `_parse_price_map` levanta `ValueError` em **toda** chamada de billing —
checkout, portal, webhook e a tela de uso caem juntos.

## 5.1 Validação (local, 2026-09-03)

- brain-api: `uv run pytest -q` → **538 passed** (antes: 535 + 1 falha, o
  `test_assignable_plan_ids_has_no_reserved_slots`, que trava justamente o conjunto de
  planos). Dois testes novos: a escada ordenada sem o combo, e um swap **para o Start**
  (que o par escrito à mão recusava com 422).
- brain-frontend: `npx tsc --noEmit` limpo · `npx vitest run` → **158 passed** ·
  `npm run build` ok. Conferido no `out/index.html` gerado: as três faixas com
  R$ 119,99 / R$ 209,99 / R$ 599,99 e 50 / 100 / 300 pré-consultas.
## 5.2 Prova em produção (2026-09-03, depois do deploy)

- **Código no ar:** `GET /openapi.json` devolve `summary: "Swap between the PreCheck tiers
  (Start/Basic/Advanced)"` na rota de upgrade — string escrita nesta rodada, então o
  container está no commit certo. (`/health` NÃO serve de prova aqui: responde igual com
  código velho.)
- **Vitrine:** `brainai.com.br` serve R$ 119,99 / R$ 209,99 / R$ 599,99, as três faixas,
  50/100/300 e o grid `price-grid--3` + `--2`. Conferido também no navegador, incluindo o
  passo de plano do `/cadastro` (que **não** aparece em `curl` — depende de
  `useSearchParams` e só existe depois da hidratação).
- **Price map:** provado pelo funil de verdade, não por leitura de env. `POST
  /public/signup-intents` com `catalog_ids: ["precheck_start"]` → **201**, e `POST
  /public/checkout-sessions` → **200** com URL do Stripe. Esse segundo passo chama
  `billing.validate_selection` como fail-fast e responde **503
  `price_not_configured:precheck_start`** quando a chave não está no map — o 200 é a prova
  de que ela está.
- Efeito colateral do teste, para limpar: tenant `ZZ SMOKE precheck_start 1788465780
  (apagar)` / `smoke-start-1788465780@example.com`, intent
  `f4160f58-1e5e-49ff-8a7a-89f1a7dd8d16`. Nenhum Customer e nenhuma assinatura nasceram
  (sessão `mode=setup`, sem pagamento); a Checkout Session expira sozinha em 04/09 20:03 UTC.

### O trial de 75 dias NÃO se aplica ao PreCheck (achado desta rodada)

`GET /public/checkout-config` devolve `trial_period_days: 75` e isso assustou com razão —
mas o número é **global**, e quem decide por compra é `billing._trial_days_for`: ele
devolve **0 para qualquer plano sem secretarIA**. O trial nunca foi cortesia comercial, é
a janela de aprovação da Meta para o WhatsApp Coexistence, e PreCheck não tem conexão
nenhuma a ser aprovada. **PreCheck cobra na hora.** Confirmado ao vivo: a Checkout Session
do teste acima voltou com `custom_text` **todo null** — o texto de trial só é montado
quando há trial. Vale para os dois caminhos (checkout direto, `billing.py:453`, e a
assinatura que o webhook cria no cold signup, `billing.py:855`).

## 6. Pendências

- [x] ~~Deploy nos dois serviços + troca do price map~~ — feito e provado (§5.2).
- [ ] **Apagar o tenant do smoke** (§5.2) — `DELETE /admin/tenants/{id}`.
- [ ] Cancelar as 3 assinaturas de teste presas no Price de R$ 1,00 (§2). Elas seguem
      cobrando R$ 1,00/mês; arquivar o Price não as tocou.
- [ ] **Preço do avulso**: R$ 2,50/pré-consulta hoje. Com o Basic a R$ 2,10/pré-consulta
      (209,99÷100) e o Advanced a R$ 2,00, o avulso está caro o suficiente para não
      canibalizar o upgrade — decisão registrada, não mexida nesta rodada.
- [x] ~~**Trial**: confirmar os 75 dias antes de vender com preço novo~~ — **resolvido,
      não era problema**: o trial não alcança plano sem secretarIA (§5.2).
- [ ] A vitrine do PreCheck (`/comecar`) segue mandando `plan=precheck_basic` fixo. Continua
      correto (é pré-seleção, e o wizard mostra as três) — mas agora a pré-seleção é a faixa
      do MEIO, o que é uma escolha comercial e não um default técnico.
- [ ] Personalização de fluxo a R$ 5/alteração (também no docx do Diogo): **fora desta
      rodada de propósito** — é produto novo (fila, orçamento, SLA), não linha de preço.
