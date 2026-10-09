# Retenção de visitas vazias do Portal — TASK-042 (lado brain-api)

Pedido do dono (2026-10-09): "cada conversa dessa que é apenas iniciada não seja gravada no banco
de forma permanente" + "nunca apague esse email digitado e nunca verificado". Registro da tarefa:
`BRAIN/tasks/TASK-042/`. Lado secretarIA: `secretarIA/docs/CHECKPOINT_retencao_visitas_portal.md`.

## Estado

Implementado e validado localmente na branch `task/TASK-042-retencao-visitas` (worktree
`BRAIN-worktrees/TASK-042/brain-api`). Commit local (SHA no `TASK.md`). Não mesclado, não
pushado, não deployado. Migração `0027_pending_retention_kept` provada só no SQLite dos testes.

## O que entrou

- `services/portal/visit_retention.py` — o job. `find_empty_visits` (filtro), `run_retention_round`
  (uma rodada limitada), `run_locked_round` (advisory lock do Postgres numa conexão AUTOCOMMIT),
  `retention_loop` (a cada `VISIT_RETENTION_INTERVAL_MINUTES`).
- `services/message_switchboard.py::discard_empty_visit` — chamada SÍNCRONA a
  `POST {secretarIA}/internal/brain-message/visits/discard`; nunca levanta exceção.
- `main.py::lifespan` — o brain-api não tem worker; o job é uma task asyncio do processo da API,
  criada só se `VISIT_RETENTION_ENABLED`.
- `config.py` — `VISIT_RETENTION_ENABLED=False`, `VISIT_RETENTION_DRY_RUN=True`,
  `VISIT_RETENTION_HOURS=24`, `VISIT_RETENTION_BATCH_SIZE=50`, `VISIT_RETENTION_INTERVAL_MINUTES=60`.
- Migração `0027` (aditiva, anulável): `message_pending_sessions.retention_kept_at`,
  `message_pending_sessions.retention_discard_requested_at`, índice em `created_at`.
- Contrato: `docs/PORTAL_MESSAGING_API.md`, seção "Retenção de visitas vazias (TASK-042)".

## Regra de "visita vazia"

Lado brain-api (filtro): `email` e `claimed_at` nulos, sem código pedido, não verificada, não
substituída, identidade sem e-mail/conta/nome, sem login, sem evento de consentimento, sem outra
visita para a mesma identidade, criada há mais de 24 h e expirada há mais de 15 min (folga para
uma mensagem enviada no último segundo ainda na fila da secretarIA). Lado secretarIA: recusa (409)
se houver qualquer mensagem do paciente, qualquer consulta (por paciente OU conversa) ou reserva viva.

**Clínicas com PreCheck são puladas inteiras.** A mesma visita pode falar com o PreCheck (aba, QR),
e o PreCheck não tem rota para dizer "esta visita está vazia". Sem isso, um visitante que respondeu
o questionário inteiro no PreCheck pareceria vazio para a secretarIA (achado C1 da revisão).
Lacuna declarada: clínica que teve PreCheck e perdeu deixa de ser pulada.

## Ordem anti-perda

1. Marca `retention_discard_requested_at` e comita (a intenção).
2. Pergunta à secretarIA.
3. `discarded` → apaga visita + identidade (guardas repetidas dentro do DELETE: e-mail digitado
   no meio do caminho impede o delete). `409` → `retention_kept_at`, nunca mais perguntado.
   `absent` numa visita já marcada numa rodada anterior → a secretarIA já fez a parte dela
   (resposta perdida, falha local) → apaga o lado brain-api. `absent` na primeira pergunta →
   a secretarIA nunca teve a visita → `retention_kept_at`. Falha → nada muda, volta na próxima
   rodada (visitas já tentadas vão para o fim da fila; 3 falhas seguidas encerram a rodada).
4. Dry-run conta TODA a fila (sem limite de lote) e não chama ninguém.

Log: uma linha `visit_retention_round` por rodada, só contagens (`candidates`, `discarded`,
`kept_not_empty`, `kept_absent`, `failed`, `precheck_clinics_skipped`). Nenhum id, nenhum e-mail.

## E-mail digitado e nunca verificado — decisão do dono e risco registrado

Nunca é apagado: nem `message_pending_sessions.email`, nem a visita que o carrega, nem
`message_patient_account_otps`. O job não toca essa tabela. **Não** zerei o `code_hash` vencido:
mexe na linha que guarda o e-mail, e o prompt mandou não mexer em nada do e-mail.

**Risco LGPD (registrado, sem ação, por decisão do dono):** um e-mail digitado errado pode ser de
outra pessoa e fica guardado sem prazo, sem que essa pessoa tenha tido contato com a clínica.

## A barreira de login

Já era o desenho e está provada por teste: digitar e-mail não cria conta nem sessão; só código
válido cria (`verify_pending_identity`), e o login do navegador só sai de `POST /pending/complete`
depois de `verified_at`. Testes em `tests/test_task042_tester.py` (Tester independente).

## Validação

Ver `BRAIN/tasks/TASK-042/TASK.md` (suítes completas, ruff, mutações que provam que os testes mordem).

## Deploy (não autorizado)

`alembic upgrade head` (0027) → `secretaria_api` + `secretaria-worker` → brain-api com
`VISIT_RETENTION_ENABLED=true` e `VISIT_RETENTION_DRY_RUN=true` → levar a contagem ao dono →
só então `VISIT_RETENTION_DRY_RUN=false`. Se o brain-api subir antes da secretarIA, a rota dá 404:
nada é apagado e a rodada para após 3 falhas.
