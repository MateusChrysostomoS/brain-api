# CHECKPOINT — sessão de conta ativa pula pendente/e-mail/código ao abrir clínica nova (2026-09-24)

Parte 1/3 de `z_prompts/PLANO_BRAIN_MESSAGE_ENTRAR_TAMBEM_SESSAO_ATIVA.md` (raiz de BRAIN),
prompt `z_prompts/PROMPT_BRAIN_MESSAGE_ENTRAR_TAMBEM_1_BRAIN_API.md`. Decisão do dono (fechada,
não reabrir): *"tendo o paciente logado no nosso portal, teria que nem perguntar o e-mail, apenas
começar a conversa e tal clínica ficará salva no portal desse paciente."*

**Estado: BUILT, não commitado, não deployado. Migração `0024` (§8, o nome) — a §1–§7 não tinha migração.** Parte 2 (secretarIA) feita em 2026-09-24 (só testes, §7); parte 3
(Brain-Message-Frontend) ainda não rodou.

## §1 — Causa raiz (reconfirmada no código antes de programar)

`api/portal/patient_access.py::open_pending` (`POST /patient-access/pending`), antes desta
rodada, depois de resolver o convite e checar o produto, fazia só isto:

```python
raw_cookie = read_patient_pending_cookie(request)          # __Host-patient_pending, só
pending = await patient_access.find_pending_by_token(...) if raw_cookie else None
...
if not resumed:
    pending, patient, raw_cookie = await patient_access.open_pending_session(session, clinic)
```

`read_patient_session_cookie` (o `__Host-patient_session`, cookie de CONTA — escrito por
`verify-otp`, `/pending/verify-otp` e `/pending/complete`) não era lido em lugar nenhum da rota.
Com conta viva no navegador, a rota ainda assim mintava uma visita anônima nova: identidade com
`email = NULL`. A secretarIA, antes do primeiro turno, pergunta ao brain-api
`POST /internal/brain-message/pending-identity`, que para essa identidade responde
`pending_unclaimed` → `AWAITING_EMAIL` → o paciente digita o e-mail → `claim_pending_email`
detecta a conta (`account_exists`) → "📧 Seu e-mail já está no nosso sistema! [...] enviei um
código de 6 dígitos" — exatamente o sintoma de 2026-09-23. O ponto onde o cookie de conta deveria
ser checado é logo depois de `require_product`/`products` e ANTES de
`read_patient_pending_cookie` — é onde o atalho entrou (`_open_with_account`).

`claim_pending_email` e a decisão "sempre pede código" de
`CHECKPOINT_portal_email_ja_cadastrado.md` §2 **não foram tocadas**: continuam valendo para quem
digita um e-mail sem cookie de conta. O caminho novo simplesmente nunca chega lá.

## §2 — Decisões tomadas sem consulta (registradas)

1. **Função extraída, não inline**: `_open_with_account(...) -> PendingSessionOut | None`, logo
   acima de `open_pending`. `None` significa "sem conta utilizável, siga como antes" para TODO
   motivo, então a rota antiga fica byte a byte igual em todos os casos de falha.
2. **`X-Brain-Client: web` é obrigatório para o atalho — e a ausência ignora o cookie, não
   recusa.** Dois motivos, um de segurança e um de deploy:
   - o cookie de conta vira credencial que ESCREVE (põe uma clínica na conta); escrita
     autenticada por cookie neste repo exige o header anti-CSRF (`auth-jwt-multitenant`),
     somado ao `SameSite=Lax` que já existia;
   - o frontend hoje em produção não manda o header nesta rota e lê `pending_token`
     incondicionalmente (`openPendingVisit` lança `502 "Visita sem token"` sem ele). Com o
     header como porta, o brain-api pode subir ANTES da parte 3 sem quebrar nenhum paciente
     logado — a resposta nova só existe para quem a pede. Novo helper
     `core/cookies.py::has_client_header` (o `require_client_header` agora o usa).
3. **Validação do cookie = `find_patient_session`**, a mesma que `confirm_sibling` e `logout` já
   usam (hash atual; não revogado, não expirado). Nenhuma segunda forma de validar sessão foi
   inventada. **Não gira o cookie**: girar é papel do `/refresh`; um segundo leitor que gira
   correria com a renovação single-flight do portal e poderia cair no detector de reuso. Efeito
   colateral aceito: um valor que acabou de ser girado (o anterior, dentro da janela de graça) não
   é aceito aqui — cai no fluxo pendente de hoje. O portal renova no mount e só depois chama
   `/pending` (sequencial), então na prática ele já carrega o valor novo.
4. **Só linhas de login com `account_id`**. Uma linha de antes do modelo de conta (0020) só
   entra numa conta quando o `/refresh` a renova (`_row_account(adopt=True)`); resolvê-la aqui pelo
   endereço seria o "atributo concede" que a skill `cross-tenant-account-linking` proíbe. Cai no
   fluxo pendente.
5. **A conta vence um cookie pendente, mesmo da mesma clínica.** Leitura literal da decisão do
   dono ("nem perguntar o e-mail"): retomar a visita pediria o e-mail de novo. A visita não é
   apagada nem reescrita; só deixa de ser alcançável por este navegador (a conversa dela continua
   na secretarIA sob o handle da visita). Raro: exige abrir o link sem conta, logar por outra
   clínica no mesmo navegador e reabrir o primeiro link.
6. **Mesmo limitador do `POST /clinics`** (`_link_limiter`, por conta), gasto só depois de a
   conta estar autenticada — entrega sessão de clínica sem código, igual àquela rota. O limitador
   por IP da rota (`_pending_limiter`) continua sendo gasto primeiro, como sempre.
7. **`add_clinic` devolvendo `None`** (identidade da clínica presa a OUTRA conta — inalcançável
   enquanto o e-mail é único por conta) cai no fluxo pendente em vez de 404: a visita não concede
   nada, e um 404 mentiria sobre o convite.
8. **Login sem clínica fixada ganha esta** (`pin_login_clinic`), como no `POST /clinics` — mantém
   estáveis os campos de transição de `_account_body`.
9. **Resposta = a clínica aberta, não a conta inteira.** O portal já tem a conta em memória (ele
   chama `/refresh` no mount) e todo `/refresh` seguinte já lista esta clínica. Devolver o
   `account_token` e a lista toda aqui só aumentaria o que um único request entrega. O formato
   plano (mesmos nomes de `ClinicSessionOut`) deixa a parte 3 reusar o parser de sessão que já
   existe (`sessionOf`).
10. **A saudação usa o gatilho que já existe, sem campo novo no fio** (ver §3, parte 2). A
    premissa do prompt — "`_greet_if_secretaria` dispara a saudação + pergunta de e-mail" — não
    se confirmou: `_greet_if_secretaria` só pede à secretarIA que abra a conversa; quem decide
    perguntar e-mail é a secretarIA, pelo probe `pending-identity`, e para uma identidade criada
    por `add_clinic` (com e-mail provado) o probe responde `verified`.

## §3 — O contrato exato (partes 2 e 3 consomem isto)

### Request — nada mudou no corpo

```
POST /patient-access/pending
Cookie: __Host-patient_session=<opaco>          ← o navegador manda sozinho (credentials: include)
X-Brain-Client: web                             ← NOVO para o frontend: sem ele, fluxo de hoje
Content-Type: application/json

{ "invite": "<código, link ou UUID>", "product": "secretaria" | "precheck" | omitido }
```

`PendingSessionIn` continua `extra="forbid"` — não há campo para nomear conta, e-mail ou
identidade (422).

### Response — `PendingSessionOut` ganhou `session_kind` e `access_token`

Conta viva + header (**novo**):

```json
{
  "session_kind": "account",
  "pending_token": null,
  "access_token": "<JWT scope=patient_message, sub=patient_ref, tenant_id, sid=login_sid=<linha de login>>",
  "token_type": "bearer",
  "expires_in": 1800,
  "tenant_id": "6f1c2d3e-0000-4000-8000-000000000002",
  "clinic_name": "Clinica Nova Unidade Teste",
  "patient_ref": "9a8b7c6d-0000-4000-8000-00000000abcd",
  "products": { "secretaria": true, "precheck": false },
  "email_claimed": false
}
```

Sem conta utilizável (sem cookie, cookie morto, sem header) — **igual a hoje**, só com os dois
campos novos preenchidos com o valor neutro:

```json
{
  "session_kind": "pending",
  "pending_token": "<JWT scope=patient_pending>",
  "access_token": null,
  "token_type": "bearer",
  "expires_in": 1800,
  "tenant_id": "6f1c2d3e-0000-4000-8000-000000000002",
  "clinic_name": "Clinica Nova Unidade Teste",
  "patient_ref": "1b2c3d4e-0000-4000-8000-00000000ef01",
  "products": { "secretaria": true, "precheck": false },
  "email_claimed": false
}
```

Regras para quem consome:

- **Leia `session_kind`**; nunca deduza o tipo pela presença de um token.
- `"account"`: `access_token` é um token de CLÍNICA comum — o mesmo que `/refresh` devolve em
  `clinics[]` para cada clínica da conta. É renovável (o próximo `/refresh` já lista esta
  clínica, com o mesmo `patient_ref`). Nenhum `Set-Cookie` sai (nem de visita, nem de conta).
  Não existe estado de e-mail/código para esta clínica: `GET /pending/status` não se aplica (o
  token é de outro escopo e é recusado lá).
- `"pending"`: exatamente o contrato de `CHECKPOINT_portal_sessao_pendente.md`.
- Erros inalterados: 404 convite, 403 produto (checado ANTES do ramo de conta — um link morto não
  adiciona nada a conta nenhuma), 429 (por IP; e, no ramo de conta, por conta).

### O que a parte 3 (Brain-Message-Frontend) precisa fazer

1. Mandar `X-Brain-Client: web` em `openPendingVisit` (hoje só o `/refresh` manda).
2. Ramificar em `session_kind`: `"account"` → guardar como sessão de clínica renovável (mesmo
   tratamento de uma clínica de `linked_sessions`/`clinics`, `origin` não-`pending`), sem
   `pendingTenantId`; `"pending"` → o código de hoje, intocado.
3. **Esperar qualquer renovação em voo antes de chamar `/pending`** (o mesmo
   `if (renewal) await renewal.promise` que `completePendingVisit` já faz). Esta rota só aceita o
   valor ATUAL do cookie (§2.3); se correr com um `/refresh` que acabou de girá-lo, recebe uma
   visita — o sintoma de volta. O boot de hoje já é sequencial (`ensurePatientAccount` →
   `enter`), mas um timer de renovação pode coincidir. Achado S1 da revisão (§4).
4. Ordem de deploy: este repo primeiro (seguro sozinho, ver §2.2), depois o frontend.

### O que a parte 2 (secretarIA) precisa fazer — provavelmente só provar

**Nenhum campo novo foi criado no `POST /internal/brain-message/open`** (`BrainMessageOpen` é
`extra="forbid"` lá; mandar um campo antes da parte 2 subir daria 422 e mataria a saudação — o
risco que `frozen-contract-migration` descreve). O sinal "esta pessoa já é conta conhecida" é o
que já existe:

```
POST /internal/brain-message/pending-identity   {tenant_id, external_id: <patient_ref>}
→ {"status": "verified"}
```

Para a identidade que `add_clinic` cria (e-mail provado, membro da conta, sem visita pendente), o
endpoint responde `verified` (provado em teste, §6). Pela leitura do código da secretarIA em
2026-09-24, `workers/tasks.py::_handle_pre_consent_identity` já trata `VERIFIED` indo direto a
`_handle_show_main_menu(source="verified_account")` e grava o consentimento local
`account_terms_verified` — sem `AWAITING_EMAIL`. **Não foi executado nem testado aqui** (outro
repo): a parte 2 deve provar que o `open` (não só o inbound) passa por esse ramo, e decidir o
nome — `patient_name` continua `null` no `open` porque o brain-api não tem nome do paciente
(a conta é só e-mail).

## §4 — Segurança

- **Uma pessoa nunca alcança outra**: a conta vem da LINHA que o cookie nomeia
  (`login_row.account_id`), nunca do request; o corpo não tem campo para nomear conta, e-mail ou
  identidade. `add_clinic` casa `(tenant, e-mail da conta)` e recusa identidade presa a outra
  conta. Teste adversarial: a pessoa B já tem identidade na clínica; o cookie da pessoa A recebe
  uma identidade NOVA de A, a de B fica com e-mail e conta de B, e a conta de B não ganha nada.
- **Bearer vivo, provado na leitura**: `find_patient_session` (revogada/expirada/desconhecida →
  fluxo de hoje), e o token emitido tem `sid = login_sid = linha de login` — um logout
  (qualquer dispositivo, `revoke_account_sessions`) mata esse token no próximo request, como
  todos os outros da conta (`_authenticate_patient`).
- **CSRF**: `SameSite=Lax` (já existia) + `X-Brain-Client` exigido para o cookie valer.
- **Sem oráculo novo**: convite inválido/clínica sem canal continuam a mesma 404, checada antes
  de olhar o cookie. O ramo de conta só responde diferente para quem já prova a conta.
- **Logs**: uma linha, `patient_pending_skipped_for_account`, só com `tenant_id` — nunca e-mail,
  handle ou id de conta (testado com gravador no `logger` do módulo).
- **Risco aceito (o mesmo de `POST /clinics`)**: quem tem o cookie de conta vivo + JS de mesma
  origem adiciona clínicas à conta e recebe tokens delas, sem código — o mesmo alcance que o
  `account_token` em memória já tinha pelo `POST /clinics` desde 2026-09-15 ("apenas
  incrementar"). Nada fora da conta fica legível.
- **Risco aceito — navegador compartilhado (achado S2 da revisão)**: a pessoa A deixou a conta
  aberta num computador da família (cookie deslizante de 90 dias); a pessoa B abre ali o link da
  clínica X. X entra silenciosamente na conta de A, a equipe de X passa a ver o e-mail de A, um
  `brain_message_channel_access` é gravado em nome de A, e B conversa como A. É a consequência
  direta da decisão do dono ("nem perguntar"); `POST /clinics` já tinha a mesma exposição via
  portal aberto. Mitigações existentes: logout encerra a conta inteira; o portal mostra o e-mail
  da conta na barra (`getPatientEmail`). Se isso incomodar, o lugar para mudar é a decisão de
  produto, não este código.
- **Visita em andamento órfã (achado S3)**: ver §2.5 — a conversa (e um agendamento feito nela)
  continua na secretarIA/clínica sob o handle da visita, mas este navegador passa a mostrar a
  identidade da conta. O cookie de visita é deixado intacto (testado), então após um logout o
  navegador volta a ela.

### Revisão de segurança (2026-09-24)

Revisor independente (`ecc:security-reviewer`, somente leitura) sobre o diff: **nenhum achado
bloqueante**. Confirmou: nenhum caminho de uma pessoa para outra; CSRF em duas camadas;
validação mais estrita que `/refresh` (não aceita o valor anterior nem linha pré-conta, não
gira); sem oráculo novo; log sem PII; sem armadilha de atributo expirado (lido antes do commit,
`expire_on_commit=False`). Tratamento: S1 → contrato da parte 3 (§3 item 3) + teste que fixa o
comportamento; S2/S3 → riscos aceitos acima; N1 (`_link_limiter` gasto antes de um `add_clinic`
que em tese devolve `None`) → mantido, inalcançável; N2 (CHECKPOINT inexistente) → este arquivo;
N3 (XSS em origem listada no CORS alcançaria esta rota) → não é novo, `/refresh` tem a mesma
confiança. Testes faltantes que ele listou foram adicionados (valor girado dentro da janela,
header com valor errado, 429 só depois de autenticar, login sem clínica fixada, cookie de visita
intacto); ficaram de fora "`add_clinic` devolvendo `None`" (exige forjar uma identidade presa a
outra conta com o mesmo e-mail, o que o índice único impede) e "visita verificada mas não
completada" (coberto pela regra geral: a conta vence qualquer visita).

## §5 — Deploy

Sem migração. Ordem: **brain-api (este) → Brain-Message-Frontend (parte 3)**; a parte 2 pode ir
em qualquer ordem porque nenhum campo novo cruza para a secretarIA. Com só o brain-api no ar,
nada muda para o paciente (o frontend antigo não manda o header).

## §6 — Validação (comandos e resultado reais, 2026-09-24)

- Antes dos testes novos, suítes existentes afetadas, com o código novo:
  `uv run python -m pytest tests/test_patient_pending_session.py tests/test_portal_auto_greeting.py
  tests/test_patient_account_invites.py tests/test_patient_access.py tests/test_refresh_cookie.py -q`
  → **182 passed**.
- Testes novos: `tests/test_patient_pending_account_shortcut.py` → **22 passed** (18 na
  primeira versão + 4 depois da revisão: valor girado → visita sem revogar a conta, header com
  valor errado, orçamento por conta só depois de autenticar + 429, login sem clínica fixada
  ganha esta). Cobrem: o
  sintoma (sem visita, sem `pending_token`, sem cookie de visita, `add_clinic` uma vez, token
  utilizável em `/threads`, probe `pending-identity` = `verified`, `/refresh` lista a clínica);
  saudação da secretarIA com o handle da conta; link PreCheck abre PreCheck; produto ausente →
  403 sem tocar a conta; idempotência (F5 e clínica de origem: mesmo handle, zero identidade ou
  consentimento novo); sem cookie → visita; conta viva sem header → visita e conta intacta;
  cookie revogado/expirado/desconhecido/pré-conta sem cookie pendente → visita nova; conta vence
  cookie pendente da mesma clínica; adversarial entre duas pessoas; 4 campos de corpo que
  tentariam nomear conta → 422; log sem PII.
- **Prova de que os testes pegam o bug**: com o atalho desligado por mutação temporária
  (`if with_account is not None and False`), 6 testes falham (os positivos, incluindo o do
  sintoma) e 12 passam (os de fallback/recusa, que devem passar nos dois casos). Mutação revertida.
- `uv run ruff check` nos 4 arquivos tocados → limpo. `ruff check .` do repo tem 13 erros, todos
  pré-existentes em arquivos que esta rodada não tocou. `ruff format` aplicado nos arquivos
  tocados (em `core/cookies.py` isso incluiu 2 linhas em branco que já faltavam no HEAD).
- Suíte completa `uv run python -m pytest -q` → **840 passed, 2 skipped, 0 falhas** (18 min;
  rodada com a primeira versão do arquivo novo; os 4 testes acrescentados depois da revisão
  passaram no arquivo isolado, 22/22).

## §7 — Pendências e o que NÃO foi feito

- Não commitado, não deployado.
- Prova ao vivo não feita (depende da parte 3 mandar o header).
- ~~Parte 2 (secretarIA): provar o ramo `verified` no `open` e decidir o nome (§3).~~ Feita
  2026-09-24: `verified` no `open` → frame + menu. O nome: ver §8 (o dono reverteu o "não pergunta
  o nome" — `secretarIA/docs/CHECKPOINT_portal_conta_ativa_abre_clinica.md`).
- Parte 3 (frontend): header + ramificação por `session_kind` (§3).

## §8 — O nome do paciente acompanha a conta (2026-09-24, mesma sessão da parte 2)

**Pedido do dono:** *"É necessário que de algum jeito a clínica tenha o dado do nome para tanto
colocar no evento da consulta, quanto em algumas mensagens do fluxo"* — escolhidas as "camadas 1 +
2": o nome mora na conta e vem no `open`; a secretarIA só pergunta quando a conta não tem nenhum.
Invalida a nota da §3 "o brain-api não tem nome do paciente (a conta é só e-mail)".

**O que entrou aqui:**

- `models/patient_access.py::MessagePatient.name` + `name_updated_at` (migração
  `0024_message_patient_name`, aditiva, nullable, sem backfill). Na IDENTIDADE (por clínica), não
  na conta: no Portal o nome é perguntado antes do código (visita pendente, ainda sem conta) e a
  identidade mantém o id quando vira conta — então o nome já está lá quando o vínculo nasce.
- `services/portal/patient_access.py`:
  - `account_display_name(account_id)` — nome mais recente DIGITADO entre as identidades da conta
    (`name_updated_at` desc; nome copiado não tem timestamp e nunca passa na frente). Só por
    `account_id`.
  - `add_clinic` copia esse nome para a identidade nova quando ela não tem nome.
  - `account_name_for_proven_email(email)` — usado só depois de um código provar o endereço.
  - `set_identity_name(tenant_id, patient_id, name)` — `tenant_id` tem que casar.
- `api/portal/internal.py`: nova rota `POST /internal/brain-message/patient-name`; `pending-otp/verify`
  devolve `patient_name` (aditivo, também no retry idempotente).
- `api/portal/patient_access.py::_greet_if_secretaria(patient_name=...)` — os dois gatilhos de
  conta (`POST /clinics` e `_open_with_account`) mandam `patient.name` no `open`. A visita anônima
  continua mandando `null`.

**Segurança:** nome é PII — nenhuma linha de log o carrega (teste com `caplog`, restrito aos loggers
da aplicação; o trace DEBUG do driver SQLite ecoa parâmetros e foi excluído de propósito). Uma pessoa
nunca recebe o nome de outra (teste adversarial), e uma linha não vinculada com o mesmo e-mail não é
lida (atributo ≠ vínculo). Retornar o nome na verificação é o mesmo alcance do `/pending/complete`
que o código já autoriza.

**Testes:** `tests/test_patient_account_name.py` (16): rota (salva, chave errada/desconhecida 404,
corpo estrito 422 ×4, sem chave 401); o sintoma (o `open` da próxima clínica leva o nome; conta sem
nome → `null`; visita anônima → `null`); só pela conta (mais recente digitado vence, outra pessoa,
linha não vinculada); caminho do código (verificação traz o nome; código errado não); sem PII em log.
`tests/test_patient_pending_session.py` — a asserção do corpo exato do verify ganhou
`"patient_name": None`.

**Validação:** suítes afetadas (`test_patient_pending_session`, `test_portal_auto_greeting`,
`test_patient_account_invites`, `test_patient_access`, `test_refresh_cookie`,
`test_patient_pending_account_shortcut`, `test_patient_account_name`) → **220 passed**. Suíte
completa `uv run python -m pytest -q` → **860 passed, 2 skipped, 0 falhas** (29 min). Mutação: com
a cópia do nome em `add_clinic` desligada, 2 testes falham (o do sintoma e o de precedência);
revertido (`cmp` contra o backup). `ruff check` nos arquivos tocados limpo; `ruff format` só
aplicado onde o HEAD já estava formatado (`api/portal/internal.py`, arquivo de teste novo).

**Revisão de segurança (2026-09-24, `ecc:security-reviewer`, somente leitura, nos dois repos):
nenhum achado bloqueante.** Confirmou: nome só atravessa clínicas por `account_id`;
`account_name_for_proven_email` só é alcançado depois de um código provar o endereço (inclusive o
retry idempotente, preso à mesma visita); `set_identity_name` recusa `tenant_id` divergente; nenhum
log carrega o nome; o nome vindo do brain-api é mascarado antes do LLM (`Patient.name` →
`load_pseudonymizer`); sem oráculo novo; `AWAITING_NAME` de paciente com consentimento tem saída
por tempo e vai ao menu. Nota não bloqueante, pré-existente: a chave interna é uma só para todas as
clínicas, então o isolamento de `patient-name` depende da secretarIA mandar o `tenant_id` certo —
igual a toda rota `/internal/brain-message/*`.

**Deploy:** `alembic upgrade head` → brain-api → secretarIA API + worker. O brain-api mapeia as
colunas novas em toda leitura de `message_patients`, então **a migração vem antes** do código.
