# CHECKPOINT — sessão pendente: o paciente conversa e agenda antes de provar o e-mail (2026-09-16)

Origem: `TECH/BRAIN/z_prompts/PROMPT_BRAIN_MESSAGE_PORTAL_SESSAO_PENDENTE.md` (onda 1 de
`PLANO_LOGIN_SEM_GATE_PACIENTE_NOVO.md`, decisões fechadas com o dono em 2026-09-16). Irmão da
mesma onda, em outro repo e sem dependência: `PROMPT_BRAIN_MESSAGE_PRECHECK_LINK_DIRETO_ABERTO.md`
(PreCheck). Consome quem vier depois: `PROMPT_BRAIN_MESSAGE_SECRETARIA_EMAIL_OTP_INLINE.md`
(onda 2, secretarIA) e `PROMPT_BRAIN_MESSAGE_PORTAL_CHAT_SEM_GATE.md` (onda 3, frontend).

**Estado: COMMITADO (`5a2d5e6 feat(patient-access): implement pending visit flow allowing chat
before email verification`) e DEPLOYADO. Migração `0021_patient_pending_sessions` CONFIRMADA
aplicada em produção — provado ao vivo em 2026-09-16 (§11), não só suposto.** Provas locais com
os comandos: §9; prova em produção (requests/responses reais contra
`https://secretaria-brain-api.cpux9k.easypanel.host`): §11.

Este checkpoint **não substitui** `CHECKPOINT_portal_clinicas_convite.md`: o modelo de conta
(conta = e-mail, clínica entra por convite, `MessagePatient.id` imutável) continua valendo
inteiro. O que muda é só o caminho de um paciente que **ainda não tem conta nenhuma**.

## §1 — O que o dono fechou (não reabrir)

> "O paciente entrará no link e não passará por nenhum processo de autenticação, irá direto para
> o chat e a secretarIA (...) pede o email do paciente - etapa 1 da autenticação, depois manda o
> LGPD e segue com o fluxo de marcação de consulta normal, quando acabar - marcar a consulta em
> si, envia-se uma mensagem pela secretarIA, avisando sobre o código e no próprio chat faz-se a
> autenticação."

> "apenas faça com que fique funcional o link para abrir a conversa do precheck da clínica (...)
> ele estará aberto mesmo, sem autenticação nenhuma por meio desse link."

> Sobre o gate "PreCheck só depois de agendamento confirmado": **"Todos os produtos, sem gate"**.

Ordem dos fatos, que é o que o código implementa:

```
link → chat → e-mail digitado na conversa → LGPD → CONSULTA MARCADA DE VERDADE → código
```

A consulta é marcada **antes** do código. O código não é trava do agendamento — é o que dá
acesso persistente à conta depois.

## §2 — Causa raiz: nada disso existia

Até a `0020`, **nenhuma identidade de paciente nascia sem um código verificado**. `verify-otp`
era o único lugar que criava `MessagePatient`, e `MessagePatient.email` era `NOT NULL`. Grep por
`pending`/`anonymous`/`provisional` em `src/` não achava nada equivalente (só o `pending` de
signup intent e o de OTP challenge, outra coisa). Então não havia o que reaproveitar: um
visitante sem e-mail não tinha handle, e sem handle a secretarIA não tem `external_id` nem o
PreCheck tem `session_ref` — não há conversa.

## §3 — Modelo escolhido: tabela própria para a VISITA, identidade sem endereço

| Peça | O quê |
|---|---|
| `message_pending_sessions` (nova) | a **visita**: `patient_id` (o handle), `tenant_id`, `email` **reivindicado e não provado**, `claimed_at`, `token_hash`, `expires_at`, `verified_at`, `superseded_by`, `revoked_at` |
| `message_patients.email` | passa a ser **anulável** — a identidade (o handle) nasce junto com a visita, sem endereço |

**Por que tabela própria e não uma linha de `message_patient_sessions` com `account_id` nulo:**
aquela forma **já significa outra coisa** — um login de antes do modelo de conta, cuja identidade
entra na conta do endereço quando o cookie daquele login renova
(`services/portal/patient_access.py::_adopt`). Um visitante pendente não tem endereço para entrar em
nada. Juntar as duas na mesma tabela transformaria essa distinção em "leia três colunas
anuláveis na ordem certa"; separadas, ela é estrutural e `_authenticate_patient` / `/refresh`
nunca veem uma visita.

**Por que `email` anulável não afrouxa nada:** toda consulta do maquinário de conta (`_adopt`,
`add_clinic`, `account_clinics`, `revoke_account_sessions`) casa `email = <alguma coisa>`, e
`NULL` não satisfaz `=` em SQL. Uma identidade pendente é, por construção, **invisível** para a
conta até ser adotada. A unique `(tenant_id, email)` continua valendo para endereços reais —
Postgres e SQLite tratam NULLs como distintos, então uma clínica pode ter muitas linhas pendentes
e no máximo uma identidade por endereço.

**O e-mail da visita é REIVINDICADO, não provado.** Ele fica em `message_pending_sessions.email`,
nunca na identidade, até um código provar. É isso que mantém um endereço não verificado fora de
toda query que significa "as clínicas desta pessoa".

## §4 — O token da sessão pendente (formato exato)

Terceiro token do paciente, e o mais restrito dos três. `core/security.py`:

```python
PATIENT_PENDING_TOKEN_SCOPE = "patient_pending"

{
  "sub":       "<MessagePatient.id>",           # o handle — external_id / session_ref
  "tenant_id": "<Tenant.id>",                   # a ÚNICA clínica que ele alcança
  "sid":       "<message_pending_sessions.id>", # relido a cada request
  "scope":     "patient_pending",
  "iat": ..., "exp": ...                        # PATIENT_TOKEN_EXPIRE_MINUTES (30)
}
```

Sem e-mail, claimed ou não: um JWT é base64, não criptografia, e aqui o endereço nem provado
está. `decode_patient_token` e `decode_patient_account_token` **recusam** este scope, e
`api/deps.py::get_current_principal` recusa qualquer token com `scope` — as três populações de
paciente e a de staff não se cruzam em nenhuma direção.

**Perna opaca:** cookie `__Host-patient_pending` (HttpOnly, Secure, `Path=/`, `SameSite=Lax`,
`max_age = PATIENT_PENDING_EXPIRE_HOURS`), só o SHA-256 no banco. Nome distinto do
`__Host-patient_session` de propósito: `/patient-access/refresh` lê só o da conta, então um valor
pendente nunca é gasto como login.

| Comparação | conta (`patient_account`) | clínica (`patient_message`) | **visita (`patient_pending`)** |
|---|---|---|---|
| nasce de | código verificado | login de conta | **abrir o link, nada mais** |
| alcança | `/clinics`, `/logout` | threads de 1 clínica | **threads de 1 clínica** |
| linha que nomeia | `message_patient_sessions` | `message_patient_sessions` | **`message_pending_sessions`** |
| cookie | `__Host-patient_session` | (usa o da conta) | **`__Host-patient_pending`** |

## §5 — Contrato HTTP novo (completo, com exemplos)

### 5.1 `POST /patient-access/clinics/lookup` — o que a clínica oferece, ANTES de qualquer login

Sem autenticação. Corpo (e não query string) porque o convite pode ser uma URL inteira colada, e
URL dentro de URL entra em todo log de acesso do caminho.

```http
POST /patient-access/clinics/lookup
{"invite": "https://portal.example/clinicas/?convite=ABCD2345"}
```
```json
{
  "tenant_id": "7f1c...",
  "clinic_name": "Clínica Exemplo",
  "products": {"secretaria": true, "precheck": true}
}
```

`products` é `EntitlementOut.products` **interseccionado com o canal**
(`message_switchboard.available_products`) — a mesma conta que `GET /patient-access/threads` faz
depois do login, então o toggle que o visitante vê antes não pode discordar das abas que ele
recebe depois. Plano, status, limites, uso e add-ons **não saem**: quanto a clínica paga não é
assunto de visitante, e `false` por "nunca comprou" é indistinguível de `false` por "assinatura
caiu" — que é a resposta honesta nos dois casos.

Recusa: `404 clinic_invite_not_found` para inválido, desconhecido e canal desligado, igual.

### 5.2 `POST /patient-access/pending` — abrir (ou retomar) a conversa

Sem autenticação.

```http
POST /patient-access/pending
{"invite": "ABCD2345", "product": "precheck"}   // product é opcional
```
```json
{
  "pending_token": "<JWT scope=patient_pending>",
  "token_type": "bearer",
  "expires_in": 1800,
  "tenant_id": "7f1c...",
  "clinic_name": "Clínica Exemplo",
  "patient_ref": "3ab9...",
  "products": {"secretaria": true, "precheck": true},
  "email_claimed": false
}
```
`Set-Cookie: __Host-patient_pending=...` na primeira vez.

- `patient_ref` **já é** o `external_id` da secretarIA e o `session_ref` do PreCheck (`bm:<id>`
  do lado do PreCheck). É esse id que tem que sobreviver ao código.
- `product`, quando vem, é checado com `message_switchboard.require_product` — o **mesmo**
  gate do relay — então um link direto de PreCheck para clínica sem PreCheck morre na porta
  (`403 product_unavailable`) em vez de na primeira mensagem.
- **Retomar:** apresentar um cookie pendente vivo **da mesma clínica** devolve a mesma visita e o
  mesmo handle, sem criar nada. É o que faz um F5 não começar uma segunda conversa.
- Recusa: `404 clinic_invite_not_found` (mesma resposta de sempre); `429` no orçamento por IP.

### 5.3 `POST /internal/brain-message/pending-email` — a secretarIA entrega o e-mail

**Perna de serviço** (`X-Internal-Api-Key`, o mesmo par que todo `/internal/*` usa). Nunca o
navegador — §6.1 explica por quê.

```http
POST /internal/brain-message/pending-email
X-Internal-Api-Key: <SECRETARIA_API_KEY>
{"tenant_id": "7f1c...", "external_id": "3ab9...", "email": "paciente@exemplo.com"}
```
```json
{"status": "claimed"}
```

`external_id` é o nome que **o chamador** usa (é o que a secretarIA recebe em cada mensagem
relayada); internamente é `patient_ref`. `tenant_id` e `external_id` têm que casar **os dois** com
a visita, então a chave de uma clínica não move um endereço para a conversa de outra.
Reivindicar de novo **sobrescreve** (o paciente corrigiu um typo — é o que acontece num chat).

Recusa: `404 pending_session_not_found` para handle desconhecido, clínica errada, visita expirada
e visita já verificada — uma resposta só.

### 5.4 `POST /patient-access/pending/request-otp` — mandar o código

Bearer = pending token. **Sem corpo.**

```http
POST /patient-access/pending/request-otp
Authorization: Bearer <pending_token>
```
```json
{"detail": "Se esse e-mail puder entrar por aqui, enviamos um código para ele."}
```

O endereço usado é o da visita (§5.3). Mesmas regras de OTP do login de conta: 6 dígitos,
10 minutos, 5 tentativas, limitador por IP **e** por endereço.

Recusa: `409 pending_email_missing` quando a conversa ainda não capturou endereço — a única
recusa aqui que o cliente pode **agir em cima** ("peça o e-mail no chat primeiro").

### 5.5 `POST /patient-access/pending/verify-otp` — virar conta de verdade

Bearer = pending token. Corpo: só o código (`extra="forbid"`).

```http
POST /patient-access/pending/verify-otp
Authorization: Bearer <pending_token>
{"code": "123456"}
```

Responde **`PatientAccountOut`, o mesmo corpo do `verify-otp` de sempre** — um cliente que já
fala o contrato de conta não precisa de nada novo — e troca os cookies: `__Host-patient_session`
entra, `__Host-patient_pending` sai.

Ordem interna (cada passo é código que já existia):

1. clínica ainda com o canal ligado? — checado **antes** de gastar o código (um código bom nunca
   é queimado por uma clínica fechada);
2. `verify_account_otp` sobre o endereço **capturado**;
3. `open_account` — a conta do endereço, criada se for a primeira;
4. `adopt_pending_identity` — o handle da visita **assume o endereço**, então `patient_ref` é o
   mesmo antes e depois do código;
5. `add_clinic` — a clínica entra na conta e o consentimento `brain_message_channel_access` é
   gravado **exatamente uma vez**, pelo mesmo `INSERT … SELECT … WHERE NOT EXISTS` de sempre;
6. sessão de conta emitida, visita fechada (`verified_at` + `revoked_at`).

Quando o passo 4 recusa (§6.5), o `patient_ref` de topo é o handle antigo da clínica e **difere**
do pendente — essa diferença É o sinal para o cliente trocar de thread.

Recusa: `400` (código errado/expirado/usado, e clínica fechada — indistinguíveis), `409`
(sem endereço capturado), `429`.

### 5.6 As rotas de thread aceitam os dois tokens

`GET /threads`, `POST /threads/{product}/messages` e `GET /threads/{product}/messages` passam a
depender de `get_thread_patient`, que aceita **token de clínica OU token pendente**. Nada mais
muda: `tenant_id` e o handle saem da identidade nos dois casos, e nenhum dos dois tokens nomeia
clínica que não seja a sua.

## §6 — Decisões tomadas sem consultar (e por quê)

O prompt mandou decidir e documentar implementação; o que era produto já estava fechado (§1).

**6.1 O e-mail entra pela perna de serviço, não pelo navegador.** É a decisão de segurança
central. Um pending token prova *uma* coisa: "este navegador abriu esta conversa". Se ele também
pudesse **nomear** a caixa de entrada a ser verificada, segurar um seria suficiente para mirar um
código em qualquer inbox. Por isso `POST /pending/request-otp` **não tem corpo** e
`POST /pending/verify-otp` só aceita `code`.

**6.2 Uma tabela nova em vez de reaproveitar `message_patient_sessions`.** §3.

**6.3 O link direto do PreCheck é `POST /pending` com `product: "precheck"`, não uma rota
própria.** Funcionalmente é a mesma coisa — um `session_ref` fresco e um token que abre a thread
daquela clínica — e duas rotas fazendo isso divergiriam na primeira mudança. O prompt pedia "um
endpoint que devolve um `session_ref`"; é o que este devolve (`patient_ref`).

**6.4 Nada é pré-criado no PreCheck.** O condutor do PreCheck abre a sessão sozinho na primeira
mensagem (`resolve_session` é idempotente por `session_ref`) e **não existe** endpoint de "criar
sessão sem mensagem". Pré-criar exigiria relayar uma mensagem sintética que o paciente nunca
mandou. O handle basta.

**6.5 Colisão `(tenant_id, email)`: a clínica que JÁ conhece o endereço fica com a identidade
dela.** É o único ramo em que `patient_ref` muda legitimamente, e não dava para evitar: a
identidade antiga tem a slot da unique **e** o histórico de conversa dela. Nada é fundido, nenhum
id é reescrito (essa regra não tem exceção); a conta usa a identidade antiga e a visita grava
`superseded_by`. **Custo aceito:** a conversa pendente (inclusive um agendamento feito nela) fica
sob um handle que a conta não abre mais. Alcançável só quando a mesma pessoa, que já falou com
**esta** clínica por conta, volta num navegador sem cookie e refaz tudo anonimamente — o cookie
de 90 dias torna isso raro.

**6.6 O cookie pendente guarda uma visita só.** Abrir o link de outra clínica substitui. A
conversa anterior vive até expirar mas deixa de ser alcançável daquele navegador. Segurar várias
clínicas ao mesmo tempo é o que a **conta** faz, e ela precisa de endereço provado para existir.

**6.7 `PATIENT_PENDING_EXPIRE_HOURS = 24`, não 90 dias.** O que a visita carrega é conversa
inacabada, não endereço provado. 24h cobre "termino isso à noite" sem deixar linha anônima
alcançável por uma estação inteira.

**6.8 `PATIENT_PENDING_RATE_LIMIT_PER_MIN = 5`, o orçamento mais apertado do app.** `POST
/pending` **escreve** (uma identidade e uma sessão por chamada) e não há sessão pela qual chavear
— só IP. É o que separa um script de uma tabela cheia de linhas anônimas. `/clinics/lookup`
divide o mesmo balde de propósito: as duas são alcançáveis sem credencial nenhuma.

**6.9 Sem gate de agendamento em lugar nenhum.** Confirmado por leitura: `available_products`,
`require_product` e `list_threads` gateiam **só** em `channels.brain_message` + `products.*` —
nunca houve trava de agendamento do lado brain-api, então não houve o que remover. O teste
`test_precheck_is_reachable_with_no_appointment_anywhere_in_sight` prova isso nas três
superfícies (pré-login, pendente, com conta) para que não volte por acidente.

## §7 — O que entrou

| Onde | O quê |
|---|---|
| `migrations/versions/0021_patient_pending_sessions.py` (novo) | `message_pending_sessions`; `message_patients.email` anulável; downgrade que apaga identidades pendentes e seus consentimentos |
| `models/patient_access.py` | `MessagePendingSession`; `MessagePatient.email` anulável (com o porquê); parágrafo novo no docstring do módulo |
| `models/__init__.py` | exporta `MessagePendingSession` |
| `core/security.py` | `PATIENT_PENDING_TOKEN_SCOPE`, `create_patient_pending_token`, `decode_patient_pending_token` |
| `core/cookies.py` | `PATIENT_PENDING_COOKIE_NAME` + set/clear/read |
| `config.py` | `PATIENT_PENDING_EXPIRE_HOURS`, `PATIENT_PENDING_RATE_LIMIT_PER_MIN` |
| `services/portal/patient_access.py` | regra 5 nova no docstring; guarda de `email is None` em `_row_account`; `open_pending_session`, `_pending_is_live`, `find_pending_by_token`, `find_live_pending`, `claim_pending_email`, `adopt_pending_identity`, `close_pending_session` |
| `schemas/portal/patient_access.py` | `PublicProductsOut`, `ClinicLookupIn`, `ClinicPublicOut`, `PendingSessionIn`, `PendingSessionOut`, `PendingVerifyIn` |
| `schemas/portal/internal.py` | `PendingEmailClaimIn`, `PendingEmailClaimOut` |
| `api/portal/internal.py` | `POST /internal/brain-message/pending-email` |
| `api/portal/patient_access.py` | `_pending_limiter`, `_PENDING_EMAIL_MISSING`, `_authenticate_pending`, `get_thread_patient`, `get_current_pending`, `_public_products`, as 4 rotas do §5; as 3 rotas de thread passam a `get_thread_patient`; docstring do módulo reescrito para três tokens |
| `tests/test_patient_pending_session.py` (novo) | 25 testes |
| `tests/conftest.py` | zera o quinto balde (`PATIENT_PENDING_RATE_LIMIT_PER_MIN`) |
| `docs/CHECKPOINT_portal_clinicas_convite.md` | corrigido: aquele prompt está **commitado** (`bc29f76`), não "UNCOMMITTED" |
| `TECH/.claude/skills/cross-tenant-account-linking/` | seção nova: identidade antes do atributo |

## §8 — Riscos aceitos (escritos de propósito)

1. **Linhas anônimas acumulam.** Todo `POST /pending` sem cookie escreve um `MessagePatient` e
   uma `message_pending_sessions`. O limitador por IP (§6.8) é a defesa; **não há coletor de lixo
   de visita expirada** nesta rodada. Se virar volume, o corte é `DELETE FROM message_patients
   WHERE email IS NULL AND id NOT IN (SELECT patient_id FROM message_pending_sessions WHERE
   revoked_at IS NULL)` — fora de escopo aqui, registrado para não virar descoberta.
2. **Um agendamento pode ficar sem conta para sempre.** O dono fechou que a consulta é marcada
   antes do código e que o código não trava o agendamento. Se o paciente nunca verificar, a
   consulta continua marcada na secretarIA e a conversa fica só naquele navegador até a visita
   expirar. **O plano-mãe marca "prazo/expiração" como decisão de produto ainda em aberto** — não
   foi decidida aqui.
3. **Aparelho compartilhado.** Quem pegar o navegador dentro de 24h retoma a conversa pendente
   (e, se o e-mail já foi capturado, pode pedir o código — que vai para a caixa do dono do
   endereço, não para quem pediu). Mitigação real é a caixa de entrada; o cookie é HttpOnly e
   host-locked.
4. **OTP por e-mail não é AAL2** (NIST SP 800-63B-4 §3.1.3.1) — herdado do modelo de conta, sem
   mudança: compensações são o teto de tentativas, a expiração curta e os limitadores.
5. **Corrida teórica na adoção** (dois `adopt_pending_identity` do mesmo endereço na mesma
   clínica): inalcançável pelo único chamador, porque `verify_account_otp` queima o único desafio
   vivo do endereço por compare-and-swap. Documentado no docstring da função.
6. **§6.5** (colisão) e **§6.6** (um cookie, uma visita).

## §9 — Provas

```
$ uv run python -m pytest tests/test_patient_pending_session.py -q
25 passed in 13.32s

$ uv run python -m pytest -q          # suíte completa, DEPOIS
3 failed, 658 passed, 2 skipped, 12 warnings in 416.95s

$ uv run python -m pytest -q          # baseline no HEAD, ANTES de tocar em nada
3 failed, 633 passed, 2 skipped, 12 warnings in 458.27s
```

As 3 falhas são **as mesmas antes e depois** e são pré-existentes no HEAD
(`test_checkout_without_stripe_key_returns_503`, `test_get_onboarding_shape_default_state`,
`test_precheck_internal_usage_event_key_unset_403` — todas do padrão "chave/env local vaza para
um teste que espera a variável vazia"). +25 testes, zero regressão.

```
$ uv run ruff check <os 12 arquivos tocados>
All checks passed!
```

Os 3 `E501` que o `ruff` aponta em `api/internal.py` são **pré-existentes** — provado rodando
`ruff` sobre `git show HEAD:src/brain_api/api/internal.py` (mesmas 3 linhas, deslocadas em 2 pelo
import novo).

**Não provado aqui:** a migração `0021` não foi rodada contra Postgres real nesta sessão
(`tests/test_migration_0020_patient_accounts.py` só roda com `BRAIN_MIGRATION_PG_URL` apontando
para um Postgres descartável, que não existe neste ambiente). A `0021` é só `ALTER COLUMN … NULL`
+ `CREATE TABLE`, sem SQL específico de dialeto no upgrade; o `downgrade` usa
`CAST(id AS VARCHAR)`, que é Postgres/SQLite. **Rodar a migração num `postgres:16` descartável
antes do deploy.**

## §10 — Ordem de deploy

```
1. alembic upgrade head          (0021 — só alarga: coluna anulável + tabela nova)
2. deploy do brain-api
3. só então: secretarIA (passa a chamar POST /internal/brain-message/pending-email)
4. só então: Brain-Message-Frontend (chat sem gate)
```

A `0021` é compatível com o brain-api **anterior** (ele não enxerga a tabela nova e nunca escreve
`email = NULL`), então o passo 1 pode ir sozinho. O inverso não vale: o brain-api novo sobre o
schema velho quebra no primeiro `POST /pending`.

`frozen-contract-migration`: o contrato do §5 é **aditivo** — nenhuma rota existente mudou de
forma, nenhum campo saiu, e `PatientAccountOut` é reusado sem campo novo. Um cliente antigo
continua funcionando byte a byte.

**Downgrade:** pare o brain-api novo antes. Apaga identidades pendentes (`email IS NULL`) e os
consentimentos delas — uma conversa pendente em andamento se perde. Identidades já adotadas
ficam, com os mesmos ids.

## §11 — Provado em produção (2026-09-16, sessão de QA separada da implementação)

Testado ao vivo contra `https://secretaria-brain-api.cpux9k.easypanel.host` (não TestClient),
tenant real "Chrysostomo For Eyes" (`9c4fa6a5-ffdb-4adb-9fe1-9036270f1246`), que já tinha
autorização permanente para dado de teste em produção (`PROMPT_BRAIN_MESSAGE_E2E_QA_PRODUCAO.md`).

1. **Migração `0021` aplicada** — a incerteza registrada em §9 ("não provado... rodar antes do
   deploy") está resolvida: `POST /pending` gravou `MessagePatient` com `email=NULL` e uma linha
   em `message_pending_sessions` sem erro. Se a migração estivesse faltando, essa chamada teria
   sido 500 (tabela/coluna inexistente) — não foi.
2. `POST /patient-access/clinics/lookup`: invite inválido → `404 clinic_invite_not_found`; UUID
   real → `200 {"tenant_id":"9c4fa6a5-...","clinic_name":"Chrysostomo For Eyes","products":
   {"secretaria":true,"precheck":true}}`. Confirma que `resolve_invite` aceita o UUID nu (não só
   link/código) e que o canal segue ligado.
3. `POST /patient-access/pending` com `{"invite":"9c4fa6a5-...","product":"precheck"}` → `200`,
   `patient_ref=fb3b7b26-0990-4404-8e9c-b4e491f62b30`, `email_claimed=false`, cookie
   `__Host-patient_pending` setado (HttpOnly, confirmado no jar do curl).
4. **Retomada:** repetindo a mesma chamada com o cookie salvo → mesmo `patient_ref`
   (`fb3b7b26-...`) — não mintou uma segunda identidade. `MessagePatient.id` estável confirmado
   neste trecho do fluxo (a parte que dependia só de brain-api; a estabilidade PÓS-código depende
   do round-trip de OTP, item 7 abaixo).
5. **Relay ponta a ponta para o PreCheck**, usando o `pending_token` do item 3 em
   `POST /patient-access/threads/precheck/messages`: `200`, sessão nova nascida no PreCheck
   (`session_ref=fb3b7b26-...`, `clinic_id=4`, `state=LGPD_PENDING`), primeira mensagem =
   boas-vindas genéricas + cartão de LGPD (`options: ["Concordo"]`) — exatamente o contrato que
   `PreCheck/docs/CHECKPOINT_BRAIN_MESSAGE_LINK_DIRETO.md` documenta. Sem handoff, sem e-mail,
   sem nome. `GET /patient-access/threads/precheck/messages` (poll) devolveu o transcript
   completo e consistente com o que foi enviado.
6. **Fronteira do endpoint interno confirmada fechada:** `POST /internal/brain-message/pending-
   email` sem `X-Internal-Api-Key` e com chave errada → `401 {"detail":"Invalid internal API
   key."}` nos dois casos. Não é um endpoint acidentalmente aberto.
7. **NÃO testado em produção nesta rodada:** o round-trip completo de OTP (reivindicar e-mail →
   pedir código → verificar → conta criada/incrementada, com `patient_ref` estável pós-código).
   Exige a `X-Internal-Api-Key` real de produção para simular a chamada que a secretarIA faria
   (`POST /internal/brain-message/pending-email`) — deliberadamente não obtida do EasyPanel
   (`EASYPANEL_CLI_SECURITY.md` proíbe ler/exportar variável de ambiente gerenciada por lá). Os 25
   testes locais (§9) cobrem essa lógica com Postgres/SQLite de teste; falta só a confirmação ao
   vivo. Duas formas de fechar essa lacuna: (a) o dono roda o teste manualmente com a chave real,
   ou (b) o dono expõe a chave numa variável de ambiente local via `!` (nunca colada no chat) para
   uma sessão futura reusar sem que o modelo veja o valor.

## §12 — Emenda para OTP inline da secretarIA (onda 2, 2026-09-17)

> **Emenda 2026-09-21**: a resposta de `POST /internal/brain-message/pending-email` ganhou
> `account_exists`/`email_masked` (aditivos, com default) — o claim agora diz se o endereço
> já é de uma conta. Contrato e tensões em `CHECKPOINT_portal_email_ja_cadastrado.md`.


**Estado:** **COMMITADO (`main@3ee89b7`), DEPLOYADO E PROVADO EM PRODUÇÃO** (2026-09-17) — ver "Rollout comprovado" no fim desta seção. Esta seção amplia o contrato da onda
1; não altera a evidência de produção da §11, que continua valendo somente para a revisão `0021`.

A implementação inicial da onda 2 precisava consultar, pedir e verificar o OTP sem possuir o
`pending_token` HttpOnly do navegador. Como esses endpoints não existiam no contrato original, a
emenda cria uma perna interna estrita, sempre autenticada por `X-Internal-Api-Key` e sempre
vinculada ao par exato `(tenant_id, external_id)`:

| Método e rota | Entrada | Saída / erros relevantes |
|---|---|---|
| `POST /internal/brain-message/pending-identity` | `{tenant_id, external_id}` | `status = pending_unclaimed | pending_claimed | verified | unknown`; nunca devolve e-mail/token |
| `POST /internal/brain-message/pending-otp/request` | `{tenant_id, external_id}` | `200 {status:"sent"}`; `404` visita ausente; `409` sem e-mail; `429` rate limit; `503` e-mail não enfileirado |
| `POST /internal/brain-message/pending-otp/verify` | `{tenant_id, external_id, code}` | `200 {status:"verified"}`; `400` código inválido/expirado/gasto; `404/409/429` como acima |

O serviço interno **não recebe token do navegador e não devolve credencial**. No sucesso ele cria
ou abre a conta, vincula a clínica, mantém `MessagePatient.id` quando não há colisão e marca a
visita como verificada. A visita continua acessível ao único navegador que já possui seu bearer.
Esse navegador conclui a promoção por:

| Método e rota | Contrato |
|---|---|
| `GET /patient-access/pending/status` | bearer pendente → `{state: pending_unclaimed | pending_claimed | otp_sent | verified}` sem PII. `otp_sent` dura somente o TTL real do desafio. |
| `POST /patient-access/pending/complete` | bearer pendente já verificado → `PatientAccountOut`, cookie HttpOnly de conta e revogação atômica da visita; `409` antes da verificação. |

`POST /patient-access/pending/verify-otp` continua compatível para clientes antigos: verifica e faz
a mesma troca atômica em uma chamada. Uma falha depois da verificação pode ser retomada porque uma
visita verificada permanece acessível até `complete`.

### Persistência e ordem de rollout

A revisão `0022_pending_identity_inline_otp` adiciona apenas
`message_pending_sessions.otp_requested_at NULL`. O marcador é local à visita; sem ele, um OTP de
login pedido em outra aba para o mesmo e-mail colocaria esta conversa no modo de código por engano.

```
1. alembic upgrade head                    # aplica 0022, aditiva e nullable
2. deploy do brain-api desta emenda
3. deploy conjunto secretaria_api + secretaria-worker da onda 2
4. só então Brain-Message-Frontend da onda 3
```

### Provas locais desta emenda

```
uv run python -m pytest tests/test_patient_pending_session.py tests/test_patient_access.py -q
85 passed in 158.07s

uv run ruff check <arquivos Python tocados pela emenda>
All checks passed!

uv run python -m pytest -q
3 failed, 661 passed, 2 skipped, 12 warnings in 1331.29s
```

Os testes novos provam: estado sem PII; recusa cross-tenant e de campos extras; pedido/erro/retry de
OTP; recusa do serviço de e-mail retornando `503` sem prometer código; `otp_sent` expirando pelo
TTL; verificação interna sem credencial; thread ainda acessível antes
da troca do navegador; `complete` emitindo cookie de conta uma única vez e invalidando o bearer
pendente. As 3 falhas da suíte completa são exatamente o baseline já registrado na §9 e vêm de
valores do `.env` local (`STRIPE_SECRET_KEY`, Embedded Signup e `PRECHECK_API_KEY`), fora deste diff:
`test_checkout_without_stripe_key_returns_503`, `test_get_onboarding_shape_default_state` e
`test_precheck_internal_usage_event_key_unset_403`. Nenhuma etapa foi executada em produção nesta
rodada.

### Rollout comprovado — 2026-09-17

Somente o status de rollout; o contrato acima não mudou.

`0022` aplicada e este repo em `main@3ee89b7`. Do lado consumidor, `secretaria_api` e
`secretaria-worker` rodam `secretarIA/main@697c24a` (`source_fingerprint=595b1f80df8c` nos dois,
`deploy_parity=match`, `alembic_head=4c8e2a7f1b93`).

Provado na clínica QA, em navegador real, com `fetch` same-origin e sem imprimir token, cookie,
código ou endereço: `POST /patient-access/pending` → `pending_unclaimed`; claim pela perna interna
da secretarIA → `pending_claimed`; pedido de código após a consulta já persistida → `otp_sent`;
código correto → `verified`; `POST /patient-access/pending/complete` devolveu `PatientAccountOut`
com o **mesmo `patient_ref`** da visita e revogou o bearer pendente de forma atômica — a segunda
chamada responde `401`. Depois de uma recarga real da página, `POST /patient-access/refresh` apenas
com o cookie `__Host-` devolveu `200` e o mesmo `patient_ref`, com as 26 mensagens do histórico
intactas.

O E2E ficou bloqueado por um tempo num defeito da secretarIA (agendamento gravando o `patient_ref`
em `appointments.phone`, `VARCHAR(32)`), não neste repo. Detalhe em
`secretarIA/docs/CHECKPOINT_secretaria_email_otp_inline.md` e `tasks/TASK-002/TASK.md`.
