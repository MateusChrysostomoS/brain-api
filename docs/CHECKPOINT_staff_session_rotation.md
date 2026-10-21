# CHECKPOINT — sessão pessoal de equipe renovada no lugar (TASK-065, 2026-10-20)

Registro da tarefa: `TECH/BRAIN/tasks/TASK-065/`. Contrato atualizado em
`docs/STAFF_ACCOUNT_CONTRACT.md` §"Renovação da sessão pessoal (TASK-065)".

**Estado: commit LOCAL na branch `task/TASK-065-sessao-pessoal-abas` (base main 8655cec). Sem
push, sem deploy, migração `0033` NÃO aplicada em produção.**

## §1 — Sintoma e causa

Aceite em produção da TASK-060 (gestora com o portal em duas abas): a cada ~15 s `resume` 200 +
`select` 200, seguidos de 401 no `hub-token` com o token recém-emitido; ~1 min depois, "Sua sessão
expirou".

- `services/staff_accounts.py::rotate_account_session` revogava a linha `StaffAccountSession` e
  abria outra (id novo) a cada `POST /auth/account/resume`.
- Tokens clínicos de `/auth/account/select` carregam `account_sid` = id da linha, e
  `api/deps.py::get_current_principal` exige `live_session(account_sid)` → cada rotação matava os
  tokens clínicos da outra aba, que renovava e matava a primeira (pingue-pongue).
- Duas renovações em voo com o mesmo cookie: a segunda apresentava valor revogado →
  `end_account_sessions` (detecção de reuso) → conta inteira encerrada.

## §2 — O que entrou

| Arquivo | Mudança |
|---|---|
| `migrations/versions/0033_staff_session_rotation.py` | `staff_account_sessions.previous_token_hash` (String 64, nullable, índice) + `rotated_at` (tz, nullable). Aditiva, sem backfill. `down_revision = 0032_staff_membership_email`. |
| `models/staff_account.py::StaffAccountSession` | as duas colunas + docstring "renovada NO LUGAR". |
| `config.py` | `STAFF_ACCOUNT_SESSION_ROTATION_GRACE_SECONDS = 60`. |
| `services/staff_accounts.py` | `rotate_account_session` reescrita (in place, CAS, graça, reuso); `find_cookie_session` (valor atual OU recém-substituído); `end_cookie_account` usa essa busca. Retorno passa a ser `(account, row, raw | None)`. |
| `api/staff_account.py` | `resume` só escreve `Set-Cookie` quando houve rotação; `logout` usa `find_cookie_session`. |
| testes | novo `tests/test_staff_account_session_rotation.py` (13), novo `tests/test_migration_0033_staff_session_rotation.py` (3); `test_staff_account_rules.py::test_account_cookie_csrf_rotation_and_replay` e dois testes de `test_staff_account_postgres.py` ajustados ao contrato novo (§4). |

Frontend (Brain-Message-Frontend) inalterado: o corpo do `resume` é o mesmo; o navegador lida com
o cookie.

## §3 — Decisões

1. **Mesmo desenho do paciente** (`services/portal/patient_access.py::rotate_patient_session`,
   `docs/CHECKPOINT_patient_session_refresh.md`): id estável, valor gira, um valor anterior aceito
   por 60 s, compare-and-swap, sem rollback no caminho perdedor, releitura com
   `populate_existing=True` antes de decidir graça vs. roubo.
2. **Cookie na graça:** igual ao paciente — 200 **sem `Set-Cookie`**. O navegador já tem o sucessor
   da resposta que girou; escrever outro cookie só criaria corrida.
3. **Expiração:** mantida a semântica anterior — cada rotação dá `expires_at = agora +
   REFRESH_TOKEN_EXPIRE_DAYS` (antes vinha da linha nova; agora desliza na mesma linha).
   `created_at` deixa de mudar a cada resume (ninguém o lê para staff).
4. **Lock:** mantido o `FOR UPDATE` na conta e na linha (serializa com troca de senha/versão e com
   outro resume no PostgreSQL); a troca condicional é quem decide em qualquer banco.
5. **Logout reconhece o valor recém-substituído** (e o logout legado `/auth/logout`, que chama
   `end_cookie_account`): uma aba que sai carregando o valor que outra acabou de girar ainda
   encerra a conta. Não cria poder novo — quem tem um valor velho já pode forçar o reuso via resume.
6. **Graça e kill switches:** a checagem de geração (`credential_version`) vem antes da graça;
   logout/reset/desativação/`end_account_sessions` dentro da janela não ressuscitam nada.

## §4 — Mudanças de comportamento aceitas (registrar para o revisor)

- `account_token` e tokens clínicos emitidos antes de um `resume` continuam válidos até a própria
  expiração (`ACCESS_TOKEN_EXPIRE_MINUTES`, 30 min) — é o objetivo da tarefa. Antes, o resume os
  invalidava. O teste antigo que afirmava o contrário foi atualizado.
- Detecção de reuso fica limitada ao **valor imediatamente anterior** (como no paciente). Um valor
  de duas ou mais gerações atrás é só "desconhecido": 401 com cookie expirado, sem encerrar a conta.
  Antes, toda linha revogada guardava seu hash e qualquer valor velho encerrava a conta. Um ladrão
  atrasado recebe 401 de qualquer jeito; perde-se só o "alarme" nesse caso.
- Fraqueza herdada da janela (idem paciente): quem roubar e usar o cookie antes da vítima fica com
  o sucessor; a vítima é aceita por até 60 s e, depois disso, o valor dela dispara o reuso e encerra
  a conta para os dois.
- Resposta 401 de `resume` continua expirando o cookie no navegador. Uma requisição atrasada com
  valor de 2+ gerações (só possível com 3 rotações durante uma requisição em voo) apagaria o cookie
  bom daquela origem. O frontend já faz single-flight por aba
  (`Brain-Message-Frontend/lib/real/brain-session.ts::resumeAccountOnce`/`accountResumeInFlight`) e
  trata qualquer 200 do `resume` igual, com ou sem `Set-Cookie` — por isso nenhuma mudança lá. O
  comentário daquele arquivo ("brain-api treats the second presentation of a rotated value as theft")
  fica parcialmente desatualizado (agora só fora da graça); atualizar numa próxima tarefa do front.

## §5 — Provas

- Red na base (`tasks/TASK-065/red.log`): 9 falhas pelos motivos certos — token clínico 401 depois
  do resume, segundo resume com o mesmo cookie 401, paralelo `(200, 401)`, id da linha muda,
  colunas/migração ausentes. 7 testes de comportamento preservado (reuso fora da graça, logout,
  reset, revogação, desativação) já passavam na base.
- Green: os 16 testes novos (13 de sessão + 3 de migração) + suíte da área staff.
- PostgreSQL real (cluster local descartável, porta própria, DB `task057_task065`):
  `alembic upgrade head` → `downgrade -1` → `upgrade head` OK, colunas/índice conferidos com `\d`;
  `STAFF_TEST_POSTGRES_URL=... pytest tests/test_staff_account_postgres.py` → 16 passed
  (`tasks/TASK-065/pytest-postgres.log`), incluindo dois resumes concorrentes em conexões distintas:
  ambos aceitos, mesma linha, exatamente uma rotação.
- Suíte completa (`uv run python -m pytest -q`, `tasks/TASK-065/pytest-full.log`): **1535 passed,
  23 skipped, 0 failed** em 13 min (base main 8655cec: 1519 passed, 23 skipped; +16 novos). Os
  skips são os opt-in de PostgreSQL, rodados à parte acima.
- `ruff check`: 6 erros, idênticos à base (todos pré-existentes, nenhum em arquivo tocado por esta
  tarefa exceto a linha pré-existente `config.py` `BRAIN_MESSAGE_PUBLIC_URL`).

## §6 — Ordem de deploy (quando o dono autorizar)

1. `alembic upgrade head` no banco do brain-api — aplica a **0032** (se ainda não aplicada; ver
   `docs/CHECKPOINT_equipe_conta_multi_clinica.md`) e a **0033**. Sem a 0033, login/resume da conta
   pessoal falham (a coluna nova é lida/escrita).
2. Deploy do brain-api.
3. Frontend: nada a fazer.

Sessões abertas antes do deploy renovam normalmente na primeira chamada (colunas nulas). Rollback
de código para a versão anterior com a 0033 aplicada é seguro (colunas nulas ignoradas); a versão
anterior volta a ter o pingue-pongue.

## §7 — Pendências

- [ ] Revisão (Reviewer) — segurança da janela de graça e do logout pelo valor anterior.
- [ ] Deploy (0032 se pendente + 0033 → brain-api) — NÃO AUTORIZADO.
- [ ] Aceite em produção com duas abas (repetir o roteiro da TASK-060).
