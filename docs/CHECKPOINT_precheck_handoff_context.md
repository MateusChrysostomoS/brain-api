# CHECKPOINT — contexto do agendamento no hand-off (nome + serviço)

> Feature checkpoint (`CLAUDE.md` — "feature grande/multi-camada"). **Perna brain-api apenas.**
> Este é o **passo 2 de 4** de uma corrente de contrato: FEAT 37 (`PreCheck`, feito —
> commit `293677e`) → **FEAT 38 (aqui)** → **FEAT 39** (`secretarIA`: passa a mandar) →
> **FEAT 40** (n8n: passa a ler). Nada nas outras três pernas foi tocado nesta rodada.
> Espelha `PreCheck/docs/CHECKPOINT_precheck_handoff_context.md`, que é o registro da perna 1.
> Referência de contrato: `CONTRACTS.md` §12.3 + a nova §12.3.1 (ordem de deploy e PII).

**Rodada:** 2026-08-26 · **Estado:** BUILT + testado local (**514 testes verdes, suíte
completa**) · **COMMITADO + PUSHED** · **NÃO deployado** (deploy no EasyPanel é passo manual,
agora desbloqueado — §5).

## 1. O que entrou neste repo

| Peça | Arquivo |
|---|---|
| Contrato inbound | `src/brain_api/schemas/internal.py::PrecheckHandoffIn` — ganha `patient_name` e `booked_service`, ambos `str \| None = None`, `max_length=255` |
| Forward outbound | `src/brain_api/services/precheck_handoff.py::request_handoff` — 2 parâmetros **keyword-only** opcionais; monta o payload condicionalmente |
| Handler | `src/brain_api/api/internal.py::precheck_handoff` — repassa os 2 campos; **gate de entitlement inalterado** |
| Testes | `tests/test_precheck_handoff.py` — 35 testes (eram 20) |
| Contrato (doc) | `CONTRACTS.md` — linha da tabela §12, parágrafo inbound, bloco do payload outbound, bullet do lado PreCheck, e a nova **§12.3.1** |

`extra="forbid"` foi **preservado** em `PrecheckHandoffIn`. A mudança **amplia o conjunto de
campos conhecidos**; não relaxa validação nenhuma — um nome de campo errado/desconhecido continua
derrubando a requisição inteira com `422`, que é exatamente o serviço que este hop presta.

### 1.1 Duas decisões de implementação que valem registro

**a) Chave ausente, nunca `null` explícito.** Um campo não fornecido some do JSON outbound em vez
de virar `"patient_name": null`. Consequência prática: um caller que não manda nada (a secretarIA
de hoje, pré-FEAT 39) produz um payload **byte-idêntico** ao de antes desta feature — é isso que
torna o deploy desta perna sozinha um no-op, e não uma mudança.

Honestidade sobre o alcance: os dois formatos (chave ausente / `null` explícito) chegam como
`None` no schema do PreCheck, então isso é minimalismo de payload + identidade pré/pós-FEAT-38,
**não** proteção contra sobrescrita — o write path do PreCheck já se recusa a deixar um `None`
apagar valor gravado (FEAT 37 §3).

**b) `patient_name`/`booked_service` são keyword-only em `request_handoff`.** São dois opcionais
adjacentes do mesmo tipo (`str | None`): trocá-los de posição por engano é trivial e **poria o
nome do paciente dentro de um campo operacional**, que é justamente o vazamento que a §3 do
prompt manda evitar. O `*` na assinatura torna a troca impossível de escrever. Isso é um desvio
(pequeno) da assinatura literal pedida no `PROMPT_FEAT_38` §2.3 — os parâmetros e defaults são os
pedidos, só a forma de passá-los ficou restrita. O único caller existente passa os 2 primeiros
argumentos posicionalmente e não foi afetado.

## 2. Gate de entitlement — o que NÃO mudou

A decisão de "esta clínica pode receber PreCheck" continua sendo **só**
`ent.status in ACTIVE_STATUSES and ent.products.precheck`, avaliada **antes** de qualquer chamada
upstream, exatamente como antes. Os campos novos não têm gate próprio, não influenciam o gate
existente, e não geram log/branch condicional nenhum no handler — passam direto.

## 3. PII

Nenhuma linha de log deste repo ganhou `patient_name` nem `booked_service` — nem em `INFO`, nem em
`WARNING`, nem no corpo de exceção capturada. `services/precheck_handoff.py` continua logando
`tenant_id` + outcome (+ `upstream_status` no ramo de erro), e nada mais.

`booked_service` também ficou de fora por default: não é PII no mesmo grau (é dado operacional da
clínica), mas **não existe motivo operacional concreto** para logá-lo, então não foi logado — a
§3 do prompt pede exatamente que isso seja uma decisão registrada, não inércia.

O teste `test_context_never_reaches_a_log_line` falha se qualquer um dos dois valores aparecer numa
chamada de logger, cobrindo os 4 desfechos que logam neste caminho (200, 500→502, 404, erro de
rede). Ele grava o que os loggers foram **chamados** com, nos **dois** módulos que a requisição
toca (router + service): o `PrintLoggerFactory` do structlog não passa pelo logging da stdlib, então
`caplog` é cego a essas linhas (mesma observação já registrada em `tests/test_onboarding_endpoints.py`).
O teste tem uma asserção de não-vacuidade (`precheck_handoff_ok` precisa ter sido capturado) para
não passar de graça caso a captura pare de funcionar.

## 4. Verificação

- `tests/test_precheck_handoff.py`: **35 passando** (eram 20 — +15, 0 falha nova).
- Suíte completa: **514 passando, 0 falhando** (`uv run python -m pytest -q`, ~5min).
- **Mutation-test dos testes novos** (aplicado e revertido, arquivo conferido byte-a-byte depois):
  - forçar as chaves a sempre irem no payload (mesmo `None`) → **6 falhas**, entre elas
    `test_handoff_legacy_two_field_body_unchanged` e `test_handoff_explicit_null_context_is_omitted`;
  - vazar `patient_name` no `logger.info` de sucesso → **1 falha**, exatamente
    `test_context_never_reaches_a_log_line`.

  Ou seja: as asserções novas realmente mordem, não passam por vacuidade.
- **Lint:** `ruff check` acusa 3 × `E501` e `ruff format --check` quer reformatar 3 arquivos —
  **tudo pré-existente**: os mesmos arquivos no `HEAD` (`d130063`, worktree limpo) produzem
  exatamente os mesmos 3 `E501` e as mesmas 3 marcações de formatação. Zero achado novo.
  `ruff format` **não** foi rodado de propósito: reescreveria código pré-existente e alargaria o
  diff muito além desta feature.

## 5. Ordem de deploy — condição que bloqueia ESTA perna

```
1) DDL em precheckv2 (feito)  ->  2) deploy PreCheck FEAT 37 (FEITO, provado)
                              ->  3) brain-api FEAT 38 (este código)  <- DESBLOQUEADO
                              ->  4) secretarIA FEAT 39   ->  5) n8n FEAT 40
```

**Código pronto ≠ seguro para produção** — por isso o deploy desta perna esperava o FEAT 37 estar
*confirmadamente vivo* no PreCheck, não só commitado. Essa condição foi **satisfeita e verificada
em 2026-08-26**, não aceita de palavra:

```
GET https://precheckv2-precheck-api.cpux9k.easypanel.host/openapi.json   -> 200
components.schemas.PrecheckHandoffRequest.properties
  = ['brain_tenant_id', 'phone_number', 'patient_name', 'booked_service']
  patient_name / booked_service: anyOf [string(maxLength 255), null]
```

O serviço **no ar** publica os 2 campos com o `maxLength` e o shape nullable exatos do commit
`293677e` — prova direta de que o FEAT 37 está deployado, obtida do próprio serviço. (Este repo
não tem endpoint de `/build`/`source_fingerprint`, então o schema do OpenAPI é o melhor sinal
disponível; ele reflete o código carregado no processo, não um checkpoint escrito à mão.)

O que é **não-negociável** é o par 3→4: a FEAT 39 não pode ir ao ar antes desta perna estar viva.
Invertê-los derruba o hand-off inteiro **em silêncio** — `422` aqui vira
`HandoffOutcome.UNAVAILABLE` na secretarIA, sintoma idêntico ao de uma queda de infraestrutura,
numa feature que já funcionava. Detalhe completo em `CONTRACTS.md` §12.3.1 e na skill
`frozen-contract-migration`.

## 6. Pendências

- [x] Commit + push — autorizados pelo Lucas em 2026-08-26.
- [ ] Deploy no EasyPanel (brain-api) — **desbloqueado** (FEAT 37 provado no ar, §5); push não
      dispara deploy sozinho, continua sendo passo manual.
- [ ] **FEAT 39** (`secretarIA` passa a mandar os campos) — próximo passo da corrente, e o que
      finalmente faz esta perna entregar valor. **Bloqueado por**: deploy desta perna no EasyPanel
      (o do FEAT 37 já saiu).
- [ ] FEAT 40 (n8n lê as colunas).
- [ ] Smoke ao vivo ponta a ponta — herdado do FEAT 37 (§6 do checkpoint do PreCheck), ainda não
      feito em lugar nenhum da corrente.
