# CHECKPOINT — renovação silenciosa da sessão do paciente (Brain-Message) (2026-09-14)

Origem: `TECH/BRAIN/z_prompts/PROMPT_BRAIN_MESSAGE_PATIENT_SESSION_REFRESH_BACKEND.md` (gerado via
`/prompt-generator`, pedido direto do dono). Par, ainda NÃO executado:
`PROMPT_BRAIN_MESSAGE_PATIENT_SESSION_REFRESH_FRONTEND.md` (Brain-Message-Frontend), que consome o
contrato do §4 abaixo — **deployar este brain-api (com a migração 0019) ANTES de considerar o irmão
executável**. Skill aplicada: `TECH/.claude/skills/auth-jwt-multitenant/SKILL.md` (cookie `__Host-`
+ rotate-on-use + guarda CSRF), estendida com a seção "The patient population" cobrindo os três
riscos do §3.

**Estado: BUILT, migração `0019_patient_session_rotation` NÃO aplicada em produção, não deployado.**
Commit: ver `CLAUDE.md` §"Prompts pendentes" (hash registrado lá assim que existir).
Suíte: ver §6 (números finais). `tests/test_patient_access.py`: 65 (antes 52).
`ruff check` limpo nos arquivos tocados (`ruff check src tests` acusa 15 erros PRÉ-EXISTENTES em
arquivos não tocados — `api/internal.py`, `api/public_signup.py`, `services/catalog.py`,
`tests/test_precheck_billing.py` etc. — mesma situação registrada no checkpoint anterior).

## §1 — Causa raiz (reconfirmada no código atual, antes de programar)

O sintoma do dono: reabrir o portal `/conversa` pedia código de novo. Cadeia:

- `api/portal/patient_access.py` (router `/patient-access`) tinha `request-otp`, `verify-otp`, `logout`,
  `siblings/{tenant_id}/confirm`, `threads*`. **Nenhuma rota de refresh.**
- `core/cookies.py::set_patient_session_cookie` só era chamado em `verify_otp`;
  `read_patient_session_cookie` só em `logout` e `confirm_sibling` — nunca para EMITIR um token.
- `config.py`: `PATIENT_TOKEN_EXPIRE_MINUTES = 30` (JWT em memória) e `PATIENT_SESSION_EXPIRE_DAYS =
  30` (cookie + linha). O comentário de `PATIENT_LINK_CONFIRM_WINDOW_MINUTES` admitia: *"with no
  refresh route, every live patient token is already that fresh"*.
- Lado frontend (`Brain-Message-Frontend/lib/real/patient-access.ts`): o access token vive SÓ em
  memória (decisão certa) — logo um reload perdia tudo, e o cookie sozinho não abria nada.

Resultado: cookie de 30 dias que não servia para nada além de logout e confirmar irmã.

## §2 — O que entrou

| Arquivo | Mudança |
|---|---|
| `migrations/versions/0019_patient_session_rotation.py` | `message_patient_sessions.previous_token_hash` (String 64, nullable, índice) + `rotated_at` (tz). Aditiva. |
| `models/patient_access.py::MessagePatientSession` | as duas colunas + docstring "renovada NO LUGAR". |
| `config.py` | `PATIENT_SESSION_EXPIRE_DAYS = 90` (comentário reescrito: é TETO desde a última renovação, decisão do dono); nova `PATIENT_SESSION_ROTATION_GRACE_SECONDS = 60`; comentário de `PATIENT_LINK_CONFIRM_WINDOW_MINUTES` reescrito (Risco C). |
| `services/portal/patient_access.py` | `rotate_patient_session` (renova in-place, CAS, janela de graça, detecção de reuso), `reopen_linked_clinic` (reemite clínica já vinculada), `PatientSessionRenewal`, docstring de `session_is_recent`. |
| `schemas/portal/patient_access.py` | `PatientRefreshOut(PatientSessionOut)` + `linked_sessions: list[ClinicSessionOut]`. |
| `api/portal/patient_access.py` | `POST /patient-access/refresh`; helpers `_sibling_report`/`_candidates_out` (compartilhados com `verify_otp`), `_expired_patient_cookie_headers`. |
| `tests/test_patient_access.py` | seção 8, 13 testes (12 funções, 1 parametrizada ×3). |

Nada mudou em `verify_otp`, `confirm_sibling`, `logout` ou nas rotas de thread além do refactor de
descoberta de irmãs (mesma lógica, extraída para `_sibling_report`).

## §3 — Os três riscos, e qual escolha resolveu cada um

**Risco A — girar o id da sessão de login derruba clínicas-irmãs.** Escolha: **(a) + (b) juntas.**
(a) A linha de login é renovada IN PLACE: `id` nunca muda; gira só o VALOR opaco (`token_hash`
novo, o antigo vai para `previous_token_hash` com `rotated_at`) e `expires_at` desliza. Assim o
`sid` do token de login e o `login_sid` de todo token de clínica vinculada continuam apontando para
uma linha viva — um token de irmã mintado ANTES do refresh segue funcionando. (b) E o refresh também
REEMITE um token novo por clínica já vinculada (`linked_sessions`), porque no caso real (reload) o
frontend perdeu todos os tokens em memória — sem (b), (a) sozinha resolveria só o cenário "refresh
com a página ainda aberta". Prova: `test_a_linked_clinic_survives_a_refresh` (token antigo abre
`/threads` da irmã depois do refresh; token reemitido também; PreCheck do login não vaza para a irmã;
trilha de consentimento não cresce). Reemissão só para irmãs com evento
`CONSENT_KIND_ACCOUNT_LINK` (`linked_identities`) — uma só descoberta não ganha token
(`test_refresh_never_mints_a_session_for_a_clinic_that_was_only_discovered`).

**Risco B — polling concorrente dispara detecção de reuso.** Escolha: **janela de graça no
backend, mais recomendação de single-flight no frontend.** `rotate_patient_session` aceita o valor
ANTERIOR por `PATIENT_SESSION_ROTATION_GRACE_SECONDS` (60s) depois da rotação: responde 200 com access
token novo e SEM `Set-Cookie` (o browser já tem o sucessor da primeira resposta; escrever outro
cookie só criaria corrida). Fora da janela, o valor anterior é sinal de roubo — semântica do staff —
e revoga a CONTA inteira (`revoke_account_sessions`, log `patient_session_reuse_detected` só com
ids). Duas descobertas da própria suíte que mudaram o desenho durante a execução:
1. `SELECT ... FOR UPDATE` sozinho não bastava: em SQLite (ignorado) as duas requisições rotacionavam
   e o browser podia ficar com um cookie que o banco nunca guardou. A rotação virou um
   **compare-and-swap** (`UPDATE ... WHERE token_hash = <apresentado>`, só `rowcount == 1` vale);
   quem perde cai na janela de graça em qualquer banco. Sem `rollback` no caminho perdedor (o UPDATE
   não casou nada; e sob a conexão única do harness o rollback apagava a escrita do vencedor).
2. Quem perdeu o CAS ainda tinha a instância PRÉ-rotação no identity map; a segunda leitura
   devolvia atributos velhos (`rotated_at=None`) e chamava o vencedor de ladrão. A busca por
   `previous_token_hash` usa `populate_existing=True`.
Provas: `test_two_refreshes_with_the_same_cookie_do_not_lock_the_patient_out` (sequencial),
`test_two_parallel_refreshes_with_the_same_cookie_both_succeed` (`asyncio.gather`, ambos 200, conta
viva, todo cookie emitido ainda renova), `test_the_replaced_cookie_is_theft_once_the_window_closes`
(depois da janela: 401, cookie expirado, todos os tokens da conta mortos — login, irmã, login
próprio da irmã).
**Para o prompt irmão (frontend): implementar single-flight equivalente a
`lib/real/brain-session.ts::refreshInFlight` mesmo assim.** A janela de graça garante que N chamadas
concorrentes não derrubam a conta; não evita N rotações desperdiçadas quando as chamadas chegam com
mais de 60s de diferença carregando o mesmo cookie velho (ex.: tab suspensa que acorda). Não é
opcional — está registrado aqui porque o backend NÃO assume que o frontend resolve sozinho.

**Risco C — `session_is_recent`/`created_at` prende `confirm_sibling` numa sessão longa.** Decisão:
**`created_at` NÃO desliza; vincular uma clínica pela PRIMEIRA vez continua exigindo login recente.**
Raciocínio: o refresh prova posse do cookie, não da caixa de entrada; a janela de 30 min existe
exatamente para não esticar "provar uma vez" em "vincular a qualquer hora nos próximos 90 dias" (o
comentário antigo da config já dizia isso). O requisito §2.5 do prompt é sobre clínicas JÁ
vinculadas — e essas voltam pelo refresh sem janela nenhuma (Risco A-b). Comportamento para o
frontend: numa sessão antiga, `confirm` responde `401 reauthentication_required` → o cliente pede
código de novo para o login (não para a irmã), confirma, e daí em diante a irmã vem em
`linked_sessions`. Prova: `test_a_first_link_on_an_old_session_still_needs_a_fresh_code` (refresh OK
numa sessão envelhecida, confirm 401, login novo + confirm 200, sessão envelhecida de novo → refresh
traz a irmã).

## §4 — Contrato HTTP para o frontend (`POST /patient-access/refresh`)

Requisição: sem corpo, sem `Authorization`. Cookie `__Host-patient_session` (o browser manda
sozinho, via proxy same-origin `/api/brain/`). Header obrigatório `X-Brain-Client: web` (mesma guarda
CSRF de `/auth/refresh`; conferida ANTES de gastar o cookie).

Respostas:
- `200` `PatientRefreshOut` = `PatientSessionOut` (`access_token`, `token_type`, `expires_in`,
  `tenant_id`, `patient_ref`, `clinic_name`, `sibling_candidates[{tenant_id, clinic_name,
  already_linked}]`) + `linked_sessions[ClinicSessionOut{access_token, token_type, expires_in,
  tenant_id, clinic_name, patient_ref}]`. `Set-Cookie` com o cookie rotacionado (`Max-Age` = 90
  dias) — AUSENTE quando a chamada caiu na janela de graça (o browser já tem o sucessor).
  `sid` do token principal = mesmo id de antes; `login_sid` de cada `linked_sessions[i]` = esse id.
- `401 "Missing patient session"` — sem cookie (nada a expirar).
- `401 "Invalid or expired session"` + `Set-Cookie` `Max-Age=0` — cookie desconhecido, expirado,
  revogado (logout) ou reuso fora da janela (aí a conta inteira já foi revogada). O cliente deve
  cair na tela de código.
- `403 missing_client_header` — sem `X-Brain-Client`; nada foi gasto.

Fluxo esperado no boot da página: `POST /refresh` → se 200, guardar `access_token` +
`linked_sessions[*].access_token` em memória (uma clínica por token, como hoje) e listar
`sibling_candidates` com `already_linked=false` para oferecer; se 401, tela de código. Single-flight
obrigatório (§3-B). Uma clínica vinculada que desligou o canal Brain-Message não vem em
`linked_sessions` (`find_sibling_candidates` filtra `brain_message_enabled`).

## §5 — Decisões tomadas sem consulta

1. **Sem limiter novo para `/refresh`.** O `/auth/refresh` do staff usa o bucket per-IP do login;
   aqui a credencial é um valor opaco de 256 bits (não há brute-force como no código de 6 dígitos),
   replay é coberto pela detecção de reuso, e um bucket per-IP atrás do proxy do portal ou é
   spoofável ou vira um bucket só para todos os pacientes (mesmo raciocínio já registrado em
   `_link_limiter`). Custo por chamada: uma leitura indexada. Revisitar se aparecer loop de cliente.
2. **Refresh não checa `channel_open_tenant`.** Igual a `list_threads`: sessão válida numa clínica
   que desligou o canal renova normalmente e vê lista vazia; o 403 fica nas rotas de thread. Refusar
   aqui derrubaria uma conta multi-clínica inteira por causa de UMA clínica.
3. **`last_seen_at` do paciente é carimbado no refresh** (e na irmã reaberta), como no login.
4. **Linhas de sessão de irmã acumulam** (uma por refresh, vida de 30 min, como `confirm_sibling`
   já fazia). Sem varredura — o mesmo estado pré-existente.
5. **Migração obrigatória antes do deploy** (`alembic upgrade head` manual no EasyPanel, como a
   0018 em 2026-09-09). Sem ela `/refresh` 500a; todo o resto continua funcionando.
6. **`CONTRACTS.md` não documenta `/patient-access/*`** (grep: nenhuma ocorrência) — lacuna
   PRÉ-EXISTENTE do router inteiro, fora do escopo. A rota nova não foi documentada lá sozinha para
   não fingir um contrato que nunca existiu; este §4 é o contrato até essa lacuna ser fechada.

## §6 — Provas

Novos testes (seção 8 de `tests/test_patient_access.py`): `test_the_cookie_alone_reopens_the_login_clinic`,
`test_the_session_slides_to_ninety_days_on_every_refresh`,
`test_refresh_needs_the_client_header_before_spending_the_cookie`,
`test_a_missing_or_dead_cookie_is_refused_and_expired[no_cookie|unknown|logged_out]`,
`test_a_linked_clinic_survives_a_refresh`,
`test_refresh_never_mints_a_session_for_a_clinic_that_was_only_discovered`,
`test_two_refreshes_with_the_same_cookie_do_not_lock_the_patient_out`,
`test_two_parallel_refreshes_with_the_same_cookie_both_succeed`,
`test_the_replaced_cookie_is_theft_once_the_window_closes`,
`test_a_first_link_on_an_old_session_still_needs_a_fresh_code`, `test_refresh_logs_ids_only`.

Suíte completa no código final: `3 failed, 608 passed, 12 warnings in 1147.07s (0:19:07)`
(HEAD anterior registrava 598 passed; +13 desta rodada, −3 abaixo). As 3 falhas são de
AMBIENTE, não de código, e não tocam patient-access: `test_billing.py::
test_checkout_without_stripe_key_returns_503` (502 em vez de 503 — a máquina tem chave Stripe
no ambiente), `test_onboarding_endpoints.py::test_get_onboarding_shape_default_state`
(`embedded_signup.configured` vem `True` — app_id/config_id da Meta presentes no ambiente) e
`test_precheck_billing.py::test_precheck_internal_usage_event_key_unset_403` (401 em vez de 403
— `PRECHECK_INTERNAL_TOKEN` presente). Os três asseguram o ramo "não configurado" e este ambiente
local está configurado; num ambiente limpo passam. `tests/test_patient_access.py`: 65/65.

## §7 — Pendências

- [ ] `alembic upgrade head` em produção (0019) → deploy do brain-api → só então o prompt irmão.
- [ ] Prompt irmão (Brain-Message-Frontend): chamar `/refresh` no boot, single-flight, guardar
      `linked_sessions`, tratar `401 reauthentication_required` do `confirm` numa sessão antiga.
- [ ] Sem varredura de linhas de sessão expiradas (pré-existente).
