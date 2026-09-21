# CHECKPOINT — Estado da mensagem (enviado / entregue / lido / falhou), parte 2: brain-api

Prompt: `z_prompts/PROMPT_BRAIN_MESSAGE_STATUS_ENTREGA_2_BRAIN_API.md` (raiz de BRAIN), Onda 3
ordem 2 de `z_prompts/PLANO_PORTAL_API_MVP.md`. Executado em 2026-09-19.

**Estado: BUILT, UNCOMMITTED, não deployado. Sem migração e sem variável de ambiente nova.**

**Onde fica o contrato.** A parte 1 publicou o dela em
`secretarIA/docs/CHECKPOINT_brain_message_status_entrega.md`, no repo secretarIA, e não aqui como o
prompt supunha. Decisão: este arquivo, com o mesmo nome e neste repo, guarda a metade do brain-api
(o lado do paciente). Cada repo mantém `docs/` como fonte de verdade da própria metade. O resumo
das duas metades fica em `docs/PORTAL_MESSAGING_API.md` §8.5.

## 1. O que o prompt supunha e o que o código mostrou

| Suposição do prompt | Realidade |
|---|---|
| "o schema de cada mensagem na listagem ganha o campo de status" | **Não existe schema por mensagem.** `schemas/portal/patient_access.py::RelayOut` é um envelope sem tipo (`payload: dict`, `extra="allow"`), escolhido de propósito (`frozen-contract-migration`). `status`/`delivered_at`/`read_at`/`updated_at` já atravessavam sem nenhuma mudança de código. O que faltava era a prova, e agora ela é um teste. |
| "confirme se este repo tem cursor próprio" | **Não tem.** `since` é repassado opaco (`message_switchboard.py::list_messages`). Aqui nada filtra, deduplica ou reordena linhas. O único item reescrito é `attachment` (`_project_attachments`), e ele não leva o status junto (há teste). |
| "reaproveite o limitador existente de `/patient-access`, igual a enviar mensagem" | **O envio de texto não tem limitador**: só o upload tem (`_attachment_limiter`, `_pending_attachment_limiter`). Aplicar o critério da decisão 4 ("nem mais permissivo nem mais restritivo que enviar mensagem") dá **nenhum limitador novo**. Reusar o de upload tornaria a marcação mais restrita que o texto e ainda consumiria a cota de anexo. |
| corpo com `read_up_to` | O contrato da parte 1 (§4.3) usa **`up_to_message_id` xor `up_to`**, o mesmo par da rota do staff. O processo manda seguir o contrato publicado, então usei os mesmos nomes. Com isso a decisão 3 ("mapeie no schema se o nome mudar") não precisou de mapeamento nenhum. |

## 2. O que mudou

| Onde | O quê |
|---|---|
| `schemas/portal/patient_access.py::PatientReadMarkIn` | `extra="forbid"`, exatamente um entre `up_to_message_id: UUID` e `up_to: AwareDatetime` (as mesmas regras de `MessagesReadMark` da secretarIA). Não aceita tenant nem paciente. |
| `services/message_switchboard.py::READ_RECEIPT_PRODUCTS` | `{"secretaria"}` (PreCheck fora, decisão 1) |
| `services/message_switchboard.py::mark_read` | `POST /internal/brain-message/messages/read` com o corpo montado campo a campo (`tenant_id`, `external_id` e o cursor). Produto fora da lista → `{"marked": 0, "applied": false}` sem chamada de rede. |
| `services/message_switchboard.py::list_messages` | Só docstring: o estado é da secretarIA, `since` = "alterado depois de", o cliente faz upsert por `id`. |
| `api/portal/patient_access.py::mark_thread_read` | Rota nova `POST /patient-access/threads/{product}/messages/read`, com `get_thread_patient` (token de clínica ou pendente) e `require_product` antes de qualquer chamada de rede. A resposta é `RelayOut` com o payload da secretarIA. |
| `api/portal/patient_access.py::poll_thread_messages` | Descrição do `since` corrigida: "linhas **alteradas** estritamente depois", upsert por `id`. |

## 3. Contrato para o frontend (parte 3)

```http
POST /patient-access/threads/secretaria/messages/read
Authorization: Bearer <token de clínica ou pendente>
Content-Type: application/json

{"up_to_message_id": "7d1c0b6e-..."}        ← ou {"up_to": "2026-09-19T09:30:00-03:00"}
```

- `200` → `{"product": "secretaria", "payload": {"marked": 3, "applied": true}, "at": "..."}`.
  Idempotente e barata: pode ser chamada a cada poll que traga algo novo com a tela visível.
- Em `precheck` → `200` com `{"marked": 0, "applied": false}`. O portal pode marcar toda aba que
  mostra, sem saber qual produto guarda estado.
- `422`: cursor ausente, duplo ou `up_to` sem offset, id que não é UUID, **ou qualquer outro
  campo**. `tenant_id`/`external_id`/`patient_ref` no corpo são recusados, não ignorados.
- `401` sem sessão válida. `403 product_unavailable` quando o produto não foi comprado ou o canal
  está desligado. `502`/`503` como no resto de `/threads/*` (`PORTAL_MESSAGING_API.md` §8.1).
- Poll: cada mensagem traz `status` (`enviado|entregue|lido|falhou`), `delivered_at`, `read_at` e
  `updated_at`, **tal como a secretarIA mandou**. O tique só faz sentido na mensagem do próprio
  paciente (`direction == "inbound"`). **Faça upsert por `id`, nunca append.** O próximo `since`
  é o maior `updated_at` recebido, não mais o `created_at`.

## 4. Decisões tomadas sem consulta

1. **Nome e lugar do CHECKPOINT**: o arquivo irmão neste repo, com o mesmo nome (acima).
2. **Recusar em vez de ignorar** `tenant_id`/`external_id` no corpo (o prompt aceitava "ignorado/
   rejeitado"). Com `extra="forbid"`, um cliente que acha que pode apontar a marcação para outra
   conversa quebra alto em vez de "funcionar" em silêncio. É o mesmo padrão de `PatientMessageIn`.
3. **O cursor é validado aqui**, com as regras da secretarIA. Sem isso, um cursor malformado
   voltaria como 422 da secretarIA e seria achatado em `502 product_error`: erro do paciente
   disfarçado de pane.
4. **PreCheck responde "não se aplica" (200) em vez de 4xx**: é o mesmo raciocínio da parte 1 para
   paciente WhatsApp. A marcação é efeito colateral de fundo, e um erro ali seria só ruído.
5. **Token pendente também marca**: o visitante lê a mesma thread, então marca do mesmo jeito,
   no escopo da própria visita.
6. **Sem limitador próprio** (§1).

## 5. Validação

- `tests/test_patient_read_receipts.py`: **22 testes**, todos verdes. Os itens 1 a 4 do checklist
  do prompt:
  1. o poll repassa o estado sem mexer, inclusive um `status` desconhecido e uma mensagem com
     anexo, cuja referência é reescrita sem apagar o estado;
  2. a marcação chega à secretarIA com o `tenant_id`/`external_id` da SESSÃO (por id e por
     instante com offset); corpo com `tenant_id`/`external_id`/`patient_ref` → 422 e zero
     chamadas; cursor ausente, duplo, sem offset ou malformado → 422 e zero chamadas;
  3. sem sessão (3 variantes) → 401. A sessão de outra clínica marca só a própria conversa, sem
     que a clínica ou o handle da primeira apareçam no fio. Token forjado que junta paciente e
     clínica errados → 401. Canal desligado → 403. Nenhum dos três casos chama a rede;
  4. **sem cursor próprio**: com o `since` antigo, a mesma mensagem (criada ANTES do cursor,
     `updated_at` DEPOIS) volta, e o `since` chega à secretarIA byte a byte. O mesmo id repetido
     na página também é repassado como veio.
  Além disso: visitante pendente, PreCheck sem rede, e secretarIA sem a rota (ordem de deploy)
  → `502 product_error` opaco.
- `uv run ruff check` nos 4 arquivos tocados: limpo. O `ruff format --check` acusa diferença em
  `api/portal/patient_access.py` e `services/message_switchboard.py`, mas ela **já existia no HEAD**
  (conferido com `git show HEAD:<arquivo> | ruff format --check -`) e fica em linhas que esta
  mudança não toca. Os arquivos não foram reformatados, para não misturar diff.
- Revisões: `ecc:security-reviewer` não achou nada acima de LOW. O LOW é a falta de limitador,
  aceita (§1). `ecc:fastapi-reviewer` achou **1 MEDIUM, corrigido**: a rota segurava a conexão
  do pool durante o hop à secretarIA (até `SECRETARIA_TIMEOUT_SECONDS`). Agora ela faz
  `session.close()` antes da chamada, como `send_thread_message`. **Pendência herdada, fora
  deste diff:** `poll_thread_messages` (o GET do poll, a rota mais chamada) tem a mesma lacuna
  desde antes desta mudança.
- Suíte completa (`uv run python -m pytest`, SQLite em memória): **717 passed, 2 skipped, 0
  failed** em 11 min. O baseline de `41ae837` era 695 passed; os 22 a mais são os testes novos.
  A suíte foi coletada antes da correção de 3 linhas do MEDIUM. Depois dela, o arquivo novo
  rodou de novo (22 passed) e o `ruff check` passou limpo.

## 6. Deploy

Ordem: **secretarIA parte 1** (migração `8b4d2f6e1a37` → `secretaria-worker` + `secretaria_api`)
→ **este repo** → frontend (parte 3). Se o brain-api subir antes, o `GET` não muda nada (campos
ausentes continuam ausentes) e a rota nova responde `502 product_error`: a secretarIA antiga dá
404. Ninguém a chama até a parte 3 existir.

## 7. Pendências

- Commit (não pedido) e deploy (autorização explícita a cada vez).
- Parte 3 (frontend): trocar o `"enviado"` fixo pelo `status` do fio, upsert por `id` no poll,
  e chamar a rota de leitura quando a thread estiver visível.
- PreCheck fora do escopo (decisão 1).
