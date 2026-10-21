# CHECKPOINT — sessão pessoal de equipe renovada no lugar (TASK-065, 2026-10-20)

Registro da tarefa: `TECH/BRAIN/tasks/TASK-065/` (revisão de segurança em `REVIEW.md`). Contrato
atualizado em `docs/STAFF_ACCOUNT_CONTRACT.md` §"Renovação da sessão pessoal (TASK-065)".

**Estado: commits LOCAIS na branch `task/TASK-065-sessao-pessoal-abas` (base main 8655cec):
`98e1149` (renovação in place) + commit da revisão 1 (cookie atribuível à linha, logs, docs). Sem
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
| `services/staff_accounts.py` | `rotate_account_session` reescrita (in place, CAS, graça, reuso); cookie `<id da linha>.<segredo>` (`cookie_value`, `parse_cookie_value`, `_cookie_row`); `open_account_session` já emite o formato novo; `find_cookie_session` (usada por logout e `end_cookie_account`); logs `staff_account_session_reuse_detected` / `staff_account_session_grace_accepted` (só o id da sessão). Retorno de `rotate_account_session`: `(account, row, cookie | None)`. |
| `api/staff_account.py` | `resume` só escreve `Set-Cookie` quando houve rotação; `logout` usa `find_cookie_session`. |
| testes | novo `tests/test_staff_account_session_rotation.py` (27 casos), novo `tests/test_migration_0033_staff_session_rotation.py` (3); `test_staff_account_rules.py::test_account_cookie_csrf_rotation_and_replay` e dois testes de `test_staff_account_postgres.py` ajustados ao contrato novo; um teste PostgreSQL novo (ladrão com duas rotações). |

Frontend (Brain-Message-Frontend) inalterado: o cookie é `HttpOnly` e opaco para ele (conferido por
grep: nenhum código lê `__Host-staff_account`); o corpo do `resume` é o mesmo. No brain-api só
`api/staff_account.py` (resume/logout) e `api/auth.py` (logout legado → `end_cookie_account`) leem o
valor, e ambos passam pelo mesmo parser.

## §3 — Decisões

1. **Base: desenho do paciente** (`services/portal/patient_access.py::rotate_patient_session`,
   `docs/CHECKPOINT_patient_session_refresh.md`): id estável, segredo gira, segredo anterior aceito
   por 60 s, compare-and-swap, sem rollback no caminho perdedor, releitura com
   `populate_existing=True` antes de decidir graça vs. roubo.
2. **Cookie atribuível à linha (revisão H1):** o valor é `<id da linha>.<segredo>`; só o hash do
   segredo é guardado (`token_hash`/`previous_token_hash`). Resume carrega a linha pelo id:
   segredo atual → gira; segredo anterior dentro da graça → aceita sem girar; **qualquer outro
   segredo para uma linha da geração viva** (anterior fora da graça, de 2+ rotações atrás, lixo) →
   reuso → `end_account_sessions` + 401. Restaura a garantia da base: um ladrão que gira duas vezes
   não desliga a detecção — o próximo resume da vítima encerra a conta para os dois.
3. **Valores que não nomeiam linha** (id malformado, parte faltando, id inexistente, cookie vazio):
   401 simples, nada encerrado. Linha expirada ou de geração antiga: 401 simples (como a base).
4. **Compatibilidade:** cookie antigo (só o segredo, sem ponto — `token_urlsafe` nunca tem ponto) é
   achado por hash (atual ou anterior), gira no lugar e sai no formato novo; daí em diante vale a
   regra 2. O valor antigo, já como anterior, ganha a graça e depois dispara o reuso.
5. **Cookie na graça:** igual ao paciente — 200 **sem `Set-Cookie`**.
6. **Logout** (e o logout legado `/auth/logout`) usa o mesmo parser e só age sobre o que o cookie
   PROVA (segredo atual ou anterior, sem limite de tempo): encerra a conta. Segredo errado ou valor
   malformado não encerra nada no logout — a detecção de reuso mora no resume.
7. **Expiração:** cada rotação dá `expires_at = agora + REFRESH_TOKEN_EXPIRE_DAYS` (mesma vida
   deslizante de antes). `created_at` deixa de mudar a cada resume (ninguém o lê para staff).
8. **Lock:** `FOR UPDATE` na conta e na linha (ordem conta → sessão, igual a `end_cookie_account`,
   `end_account_sessions` e à recuperação de senha); a troca condicional decide em qualquer banco.
9. **Graça e kill switches:** a checagem de geração (`credential_version`) vem antes da graça;
   logout/reset/desativação/`end_account_sessions` dentro da janela não ressuscitam nada.
10. **Logs (revisão L2):** `staff_account_session_reuse_detected` (warning) e
    `staff_account_session_grace_accepted` (info) com `session_id` apenas — nunca valor/hash de
    cookie, e-mail ou `account_id` (o contrato já proíbe account id em log de operação pessoal).

## §4 — Mudanças de comportamento e riscos aceitos

- `account_token` e tokens clínicos emitidos antes de um `resume` continuam válidos até a própria
  expiração (`ACCESS_TOKEN_EXPIRE_MINUTES`, 30 min) — é o objetivo da tarefa. Antes, o resume os
  invalidava.
- Cenário de roubo, descrito corretamente (a versão anterior deste § estava invertida): quem usa o
  cookie roubado ANTES da vítima fica com o sucessor. Com a regra 2, o próximo resume da vítima —
  com qualquer valor que ela tenha — encerra a conta para os dois, mesmo que o ladrão tenha girado
  várias vezes. Dentro dos 60 s da graça a vítima é aceita sem alarme (L1 abaixo).
- **Novo trade-off do formato:** quem conhecer o id da sessão (vai no `sid` do `account_token` e no
  `account_sid` dos tokens clínicos; não é segredo) pode chamar `resume` com `<id>.lixo` e encerrar a
  conta — só negação de serviço (sai todo mundo), nunca acesso. Quem tem um desses tokens já
  consegue o mesmo via `POST /auth/account/logout` com o bearer. O `resume` tem CSRF e o limite por
  IP do login. Alternativa sem esse efeito (seletor aleatório próprio em coluna nova, fora dos
  tokens) fica como follow-up se o dono preferir.
- Riscos LOW da revisão aceitos:
  - **L1** — a graça entrega `account_token` + `clinical_session` a quem reapresentar o valor
    substituído em até 60 s, sem alarme; inerente à segunda aba; logado (L2); reduzir a janela se a
    telemetria mostrar corridas de poucos segundos.
  - **L4** — resposta de rotação perdida: o navegador fica com o valor antigo; dentro de 60 s parece
    tudo certo e o primeiro resume depois da janela encerra a conta (a base encerrava na hora);
    agora visível no log de reuso.
  - **L5** — 401 do `resume` expira o cookie; uma requisição muito atrasada com valor velho agora é
    reuso (encerra a conta, como na base) — trade-off a favor da detecção.
- O comentário de `Brain-Message-Frontend/lib/real/brain-session.ts` (resume pessoal: "second
  presentation of a rotated value as theft") fica parcialmente desatualizado (agora só fora da
  graça); o front já faz single-flight por aba (`resumeAccountOnce`) e trata qualquer 200 igual.
  Atualizar numa próxima tarefa do front.
- Follow-up fora do escopo: a mesma fraqueza H1 existe no paciente (`rotate_patient_session`).

## §5 — Provas

- Red na base (`tasks/TASK-065/red.log`): 9 falhas pelos motivos certos — token clínico 401 depois
  do resume, segundo resume com o mesmo cookie 401, paralelo `(200, 401)`, id da linha muda,
  colunas/migração ausentes. 7 testes de comportamento preservado já passavam na base.
- Revisão 1: o teste que afirmava H1 como desejado foi invertido
  (`test_a_value_two_rotations_old_ends_the_account`) e falhou contra `98e1149` antes da correção;
  novos: ladrão com duas rotações via HTTP, cookie nomeia a linha, segredo errado encerra a conta,
  7 formatos malformados/desconhecidos = 401 simples, cookie legado (serviço e HTTP) volta no formato
  novo, logout ignora cookie que não prova nada, logs só com id.
- PostgreSQL real (cluster local descartável, porta própria, DB `task057_task065`):
  `alembic upgrade head` → `downgrade -1` → `upgrade head` OK; `tests/test_staff_account_postgres.py`
  → 17 passed (`tasks/TASK-065/pytest-postgres-2.log`), incluindo resumes concorrentes em conexões
  distintas (ambos aceitos, mesma linha, uma rotação) e o ladrão com duas rotações.
- Suíte completa: ver §5.1.
- `ruff check`: 6 erros, idênticos à base (todos pré-existentes, fora dos arquivos tocados, exceto a
  linha pré-existente `config.py` `BRAIN_MESSAGE_PUBLIC_URL`).

### §5.1 — Suíte completa

- `98e1149`: 1535 passed, 23 skipped, 0 failed (`tasks/TASK-065/pytest-full.log`).
- Revisão 1: a suíte completa foi INTERROMPIDA pelo Claude Code por falta de memória na máquina
  (não por falha), em ~33% com 0 falhas (`tasks/TASK-065/pytest-full-2.log`). Não reiniciada sem
  pedido. Substituto: todos os arquivos da área staff/auth/equipe/privacidade/rbac/hub →
  **234 passed, 21 skipped, 0 failed** (`tasks/TASK-065/pytest-staff-area-2.log`). **Pendente:**
  rodar a suíte completa de novo antes do merge.
- Red da revisão 1 (`tasks/TASK-065/red-review1.log`): o arquivo de testes novo contra o serviço de
  `98e1149` → 7 failed (H1, formato, legado, logs), 20 passed.

## §6 — Ordem de deploy (quando o dono autorizar)

1. `alembic upgrade head` no banco do brain-api — **obrigatória e ANTES do código**; aplica a
   **0032** (se ainda não aplicada; ver `docs/CHECKPOINT_equipe_conta_multi_clinica.md`) e a
   **0033**. Sem a 0033, toda leitura de `staff_account_sessions` falha: login/resume/logout da conta
   pessoal, **toda requisição clínica de quem entrou pela conta pessoal** (`account_sid` →
   `api/deps.py::get_current_principal`) e o hub-token da secretarIA — o portal inteiro dessas
   pessoas responde 500.
2. Deploy do brain-api.
3. Frontend: nada a fazer.

Sessões abertas antes do deploy (cookie antigo) renovam na primeira chamada e passam ao formato
novo. **Rollback de código** com a 0033 aplicada não quebra o banco (colunas nulas ignoradas), mas
o código antigo não entende cookies no formato novo (faz hash do valor inteiro): quem já renovou
precisa entrar de novo, e a versão antiga volta a ter o pingue-pongue.

## §7 — Pendências

- [ ] Re-revisão (Reviewer) da revisão 1.
- [ ] Deploy (0032 se pendente + 0033 → brain-api) — NÃO AUTORIZADO.
- [ ] Aceite em produção com duas abas (repetir o roteiro da TASK-060).
- [ ] Follow-up: H1 no paciente; comentário do front; eventual seletor aleatório no lugar do id.
