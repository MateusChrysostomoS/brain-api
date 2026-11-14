# CHECKPOINT — Rota de opções das listas longas do Portal (TASK-082)

Estado (2026-11-14, S7): **mesclado no `main` local** (merge `task/TASK-082-options-proxy`); **sem push, sem deploy.**
Contrato da rota: `docs/PORTAL_MESSAGING_API.md` §5.1 (fonte de verdade). Visão geral:
`../secretarIA/docs/CHECKPOINT_listas_paginadas.md`.

## O que entrou onde

- **Rota do paciente** `POST /patient-access/threads/{product}/messages/{message_id}/options`
  (`api/portal/patient_access.py::get_message_options`), corpo JSON `{cursor, q, page_size}` — a busca nunca vai no
  endereço. O token do paciente decide clínica e paciente; o cartão precisa ser da conversa dele.
- **Leitura do corpo à mão** (`_options_raw` + `_options_body`): corpo inválido, aninhado demais (`RecursionError`) →
  422 `invalid_options_request` sem eco; cliente que cai no meio do envio → 400. `q` normalizado (NFC, ≤40).
- **Repasse** `services/message_switchboard.py`: 404/422 do secretarIA mapeados para os códigos do contrato (sem
  vazar detalhe), resposta filtrada por lista de campos permitidos, 404 inesperado do secretarIA vira log alto.
  Limite de taxa próprio em `config.py`.
- **Nada da busca em log.**

## Prova

`tests/test_patient_options.py` (101 testes). Suíte completa na árvore mesclada: ver `tasks/TASK-082/TASK.md`
"Validation". Navegador (S7, backend real local): rolagem e busca do Portal passam por esta rota; nenhuma palavra
buscada aparece no log.

## Pendências (baixas, revisão N2/N3)

Corpo sem limite de tamanho nem checagem de `Content-Type` (rota só com token de portador); a lista de campos
permitidos filtra chaves, não tipos. Aviso de depreciação `HTTP_422_UNPROCESSABLE_ENTITY` no Starlette (cosmético).

## Deploy

Depois do secretarIA (API + worker), antes do Brain-Message-Frontend. Sem migração. Não autorizado.
