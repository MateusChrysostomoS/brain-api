# CHECKPOINT — portal do paciente: login só por e-mail + clínica entra por convite (2026-09-15)

> **Ponteiro 2026-09-21**: a detecção de "e-mail já cadastrado" na abertura do Portal
> (`CHECKPOINT_portal_email_ja_cadastrado.md`) LÊ a existência de uma conta sem vincular
> nada — mas expõe um fato de PLATAFORMA dentro do chat de UMA clínica. A tensão com o
> vínculo por convite deste documento está registrada no §4 de lá, em aberto para o dono.

Origem: `TECH/BRAIN/z_prompts/PROMPT_BRAIN_MESSAGE_PORTAL_CLINICAS_BACKEND.md` (decisões fechadas com o
dono em 2026-09-14). Irmão, a rodar DEPOIS do deploy deste: `PROMPT_BRAIN_MESSAGE_PORTAL_CLINICAS_FRONTEND.md`
(Brain-Message-Frontend), que lê o contrato do §4.

**Estado: BUILT e COMMITADO em `main` — `bc29f76 Add comprehensive tests for patient account invites
and clinic associations` (confirmado em 2026-09-16: `git log` mostra o commit e `git status` está limpo
nos arquivos desta feature). NÃO deployado, e a migração `0020_patient_accounts` continua SEM
confirmação de ter sido aplicada em produção** — não há como checar isso por código daqui, então fica
registrado como pendência em aberto, não como "aplicada". Até 2026-09-16 este parágrafo dizia
"UNCOMMITTED": era estado velho, corrigido pelo prompt da sessão pendente.
Revisões e o que mudou por causa delas: §8. Provas com os comandos: §9.

## §1 — Causa raiz (reconfirmada no código antes de programar)

O bug da auditoria (com a conta aberta, `/conversa/?clinica=<outra>` é ignorado) tem uma causa no front
(`Brain-Message-Frontend/lib/patient-account.ts::portalScreen` nunca lê a URL quando já há clínicas) e uma
causa de fundo neste repo: **o login era por clínica**.

- `schemas/portal/patient_access.py::OtpRequestIn` / `OtpVerifyIn` exigiam `tenant_id`.
- `services/portal/patient_access.py::issue_otp(session, tenant_id, email)` / `verify_otp(...)` só emitiam e
  aceitavam código para um par `(tenant_id, email)` (`MessagePatientOtp` único por esse par), e
  `_resolve_patient` criava a identidade daquela clínica.
- `models/patient_access.py::MessagePatientSession.patient_id` / `tenant_id` eram `NOT NULL`: uma sessão de
  login não existia sem uma clínica.
- As outras clínicas só chegavam por descoberta de e-mail (`find_sibling_candidates`) + confirmação
  (`confirm_sibling_link`) — exatamente o que o dono tirou do produto.

Não existia, portanto, "entrar na conta" sem escolher uma clínica, nem incrementar uma clínica numa conta
aberta sem um código novo.

## §2 — Modelo escolhido: tabela de conta (opção a)

- `message_patient_accounts` (e-mail único, minúsculo): a conta é o endereço provado por código. Existe sem
  clínica nenhuma.
- `message_patient_account_otps`: o desafio passa a ser por e-mail. A tabela antiga `message_patient_otps`
  fica intocada (o brain-api anterior ainda escreve nela na janela de deploy) e sai numa migração futura.
- `message_patients.account_id` (FK `SET NULL`): **pertencer à conta é ter `account_id`**. Nenhum id muda nem
  se funde; adicionar uma clínica REUSA a identidade que ela já tinha para o endereço (é o `external_id` da
  secretarIA e o `session_ref` do PreCheck).
- `message_patient_sessions.account_id` (FK `CASCADE`); `patient_id`/`tenant_id` viram anuláveis e passam a
  significar só "a clínica que este login nomeia" nos campos de transição (§5).
- `tenants.patient_invite_code` (8 caracteres, único, anulável).

**Quem entra na conta:** a clínica de um convite (`add_clinic`); dos dados antigos, a identidade com vínculo
confirmado (backfill da 0020) e a identidade de um login antigo quando o cookie DAQUELE login renova
(`_adopt`). Nunca uma linha só porque o e-mail casa (§6.4).

Por que (a) e não "a conta é o e-mail" (b): o join vira `account_id` indexado; há onde carimbar
`last_seen_at` da conta; a pertença é uma coluna explícita (sem ela, "casar pelo e-mail" voltaria a ser a
regra por acidente); e o índice em `message_patients.email` que faltava (§8.7 do checkpoint da conta única)
entrou junto.

## §3 — O que entrou

| Onde | O quê |
|---|---|
| `migrations/versions/0020_patient_accounts.py` | tabelas de conta e de desafio, `account_id` nas identidades e sessões, `patient_id`/`tenant_id` anuláveis, índice em `message_patients.email`, `tenants.patient_invite_code` + unique; backfill de códigos e de contas (só vínculo confirmado); recusa dialeto que não seja Postgres; downgrade. |
| `models/patient_access.py` | `MessagePatientAccount`, `MessagePatientAccountOtp`, colunas novas, docstrings do modelo novo; `CONSENT_KIND_ACCOUNT_LINK` vira histórico (só lido). |
| `models/tenant.py` | `patient_invite_code` (default `core/invite_codes.py::generate_invite_code`). |
| `core/invite_codes.py` (novo) | alfabeto, gerador, normalizador, `parse_invite` (código, UUID, link novo, link antigo), `invite_link`. |
| `core/security.py` | `PATIENT_ACCOUNT_TOKEN_SCOPE`, `create_patient_account_token`, `decode_patient_account_token`. |
| `config.py` | `BRAIN_MESSAGE_PORTAL_URL` (não secreta); comentários de `PATIENT_LINK_RATE_LIMIT_PER_MIN` e `PATIENT_LINK_CONFIRM_WINDOW_MINUTES` (não é mais lida). |
| `services/portal/patient_access.py` | reescrito: `issue_account_otp` (upsert), `verify_account_otp` (tentativa gasta por UPDATE condicional antes de comparar + queima por CAS), `open_account`/`_ensure_account`/`_adopt`, `resolve_invite`, `add_clinic` (upsert + consentimento `INSERT … SELECT … WHERE NOT EXISTS`), `account_clinics`, `login_clinic`/`pin_login_clinic`, `session_account`, sessões por conta, `rotate_patient_session` (um commit só, depois do CAS), `revoke_account_sessions(email)`. Saíram `issue_otp`, `verify_otp`, `_resolve_patient`, `session_is_recent`, `reopen_linked_clinic`, `find_sibling_candidates`, `linked_identities`, `confirm_sibling_link`. |
| `schemas/portal/patient_access.py` | `PatientAccountOut` (novo + campos de transição marcados `deprecated` no OpenAPI), `ClinicInviteIn`, `OtpRequestIn`/`OtpVerifyIn` com `tenant_id` opcional e `invite`. Saíram `PatientSessionOut`/`PatientRefreshOut`. |
| `api/portal/patient_access.py` | reescrito sobre o serviço novo; rota nova `POST /patient-access/clinics`; `confirm_sibling` vira transição (`deprecated=True`); logout por endereço, sem criar nem adotar nada. |
| `api/entitlements.py` + `schemas/entitlement.py` | `GET /entitlements/patient-invite` (`PatientInviteOut`), cunhagem condicional do código. |
| `tests/test_patient_account_invites.py` (novo), `tests/test_migration_0020_patient_accounts.py` (novo), `tests/test_patient_access.py`, `tests/conftest.py` | §9. |

## §4 — Contrato HTTP novo (para o frontend)

Dois tokens de paciente, os dois só em memória, os dois com `sid` = a linha de login (o cookie):

- **token da conta** — `scope=patient_account`, `sub` = id da conta, `sid`. Serve para `POST /clinics` e
  `POST /logout`. Não abre thread (401) nem rota de staff (401).
- **token de clínica** — `scope=patient_message`, `sub` = `patient_ref`, `tenant_id`, `sid` = `login_sid` =
  linha de login. É o único que as rotas de thread aceitam. Não adiciona clínica (401 em `/clinics`).

**Convite** (`invite`), qualquer uma destas formas, colada como veio: o código (`ABCD2345`, qualquer caixa,
com espaço ou hífen), o link novo `<portal>/clinicas/?convite=<código>`, o link antigo
`<portal>/conversa/?clinica=<uuid>` ou o UUID puro. Chaves de query lidas, nesta ordem: `convite`, `clinica`,
`codigo`, `code`, `invite`; sem elas, o último segmento do caminho.

### `POST /patient-access/request-otp`

```json
{"email": "paciente@exemplo.com"}
```

Sempre `200 {"detail": "Se esse e-mail puder entrar por aqui, enviamos um código para ele."}`. `429` pelos
limitadores de IP e de endereço (inalterados).

### `POST /patient-access/verify-otp`

```json
{"email": "paciente@exemplo.com", "code": "123456", "invite": "https://portal/clinicas/?convite=ABCD2345"}
```

`invite` é opcional. `200` + `Set-Cookie: __Host-patient_session=…` (90 dias, igual a antes):

```json
{
  "account_token": "<JWT patient_account>",
  "token_type": "bearer",
  "expires_in": 1800,
  "clinics": [
    {
      "access_token": "<JWT patient_message>",
      "token_type": "bearer",
      "expires_in": 1800,
      "tenant_id": "<uuid A>",
      "clinic_name": "Clínica A",
      "patient_ref": "<uuid>"
    }
  ],
  "invited_tenant_id": "<uuid A>",
  "access_token": "<igual a clinics[0].access_token>",
  "tenant_id": "<uuid A>",
  "patient_ref": "<uuid>",
  "clinic_name": "Clínica A",
  "sibling_candidates": [],
  "linked_sessions": []
}
```

- `clinics`: TODAS as clínicas da conta com o canal ligado, por nome. Conta nova sem convite → `[]`. Um login só
  por e-mail NÃO traz de volta clínicas antigas nunca vinculadas: elas voltam pelo link/código delas (mesmo id,
  mesmo histórico) ou pelo cookie do login antigo delas.
- `invited_tenant_id`: a clínica que o convite resolveu; `null` sem convite **ou** com convite que não serve
  (o código estava certo, o login não falha — o front mostra "Não encontramos essa clínica…").
- `400 "Código inválido ou expirado"` para código errado, vencido, usado, inexistente ou com as tentativas
  esgotadas. `422` para corpo malformado ou `invite` + `tenant_id` juntos. `429` pelo limitador de verify.

### `POST /patient-access/clinics` — incrementar a conta

`Authorization: Bearer <account_token>`, corpo `{"invite": "ABCD2345"}`.

- `200` = `ClinicSessionOut` (`access_token`, `token_type`, `expires_in`, `tenant_id`, `clinic_name`,
  `patient_ref`). Sem código, sem janela de login recente. **Idempotente**: clínica que já estava na conta
  devolve a sessão dela e não grava nada; toques simultâneos dão a mesma identidade e um consentimento só.
- `401` sem token / token inválido / token de clínica. `404 {"detail": "clinic_invite_not_found"}` para
  convite ilegível, clínica inexistente e canal desligado — a mesma resposta. `422` corpo vazio, > 512
  caracteres ou campo extra. `429` (`PATIENT_LINK_RATE_LIMIT_PER_MIN`, por conta, contado só depois da
  autenticação).

### `POST /patient-access/refresh` — contrato mantido

Cookie + `X-Brain-Client: web`, como antes. `200` = o mesmo corpo do verify (sem `invited_tenant_id`), com
`clinics` e `linked_sessions` = todas as clínicas da conta. Rotação in place, graça de 60s, 90 dias
deslizantes, reuso → conta revogada: inalterados. `401`/`403` como antes.

### `POST /patient-access/logout` — contrato mantido

Com Bearer de conta OU de clínica: a conta inteira, todas as clínicas, todos os aparelhos (por endereço). Sem
Bearer: só a linha do cookie — e, como todo token do login aponta para ela, todas as clínicas daquele login.
Sempre 200.

### `GET /entitlements/patient-invite` — staff

`Authorization: Bearer <token de staff do tenant>` (`require_tenant`, igual a `GET /entitlements`):

```json
{"tenant_id": "<uuid>", "invite_code": "ABCD2345", "brain_message_enabled": true,
 "invite_link": "https://<BRAIN_MESSAGE_PORTAL_URL>/clinicas/?convite=ABCD2345"}
```

`invite_link` é `null` enquanto `BRAIN_MESSAGE_PORTAL_URL` estiver vazia. Tenant sem código (criado pelo
brain-api anterior na janela) ganha o código nesta leitura; duas leituras simultâneas entregam o mesmo código.

**O que o front NOVO deve fazer:** guardar `account_token` + `clinics[*].access_token` em memória (sem
storage); link da clínica sem conta → `verify-otp` com `invite`; link com conta aberta, ou "Adicionar
clínica" → `POST /clinics` com o token da conta (renovar pelo `/refresh` se vencido, no mesmo voo único);
**parar de ler** `access_token`/`tenant_id`/`patient_ref`/`clinic_name`/`sibling_candidates`/
`linked_sessions` e de chamar `/siblings/*`.

## §5 — Compatibilidade na janela entre os deploys

O backend vai primeiro; o portal no ar (2026-09-14, `lib/real/patient-access.ts`) continua funcionando:

- **Corpo antigo** `{tenant_id, email}` / `{tenant_id, email, code}` = login + convite daquela clínica. O gate
  antigo segue: clínica sem canal → nenhum código enviado; no verify → `400` ANTES de gastar o código.
- **Campos de transição** (`access_token`, `tenant_id`, `patient_ref`, `clinic_name`): a clínica que o login
  nomeia (`login_clinic`) — a pela qual entrou; um login só por e-mail fica FIXADO na primeira clínica que
  tiver. **A mesma em toda renovação**: o `applyRenewal` do front antigo derruba a conta se esse topo mudar.
  Mantida mesmo que o canal dela desligue, como o refresh antigo.
- `sibling_candidates` = as OUTRAS clínicas da conta, todas `already_linked: true` (o front antigo as reabre
  sozinho via confirm, sem pergunta). Nunca uma clínica fora da conta.
- `POST /siblings/{tenant_id}/confirm` (deprecated): só reabre clínica que já é da conta; exige o token da
  clínica do login + o cookie desse login (senão `401 reauthentication_required`); fora da conta `404
  sibling_not_found` — inclusive um "Sim" dado agora a uma pergunta que a página antiga ainda guardava.
- **Linhas e tokens de antes da migração:** a 0020 põe na conta só as identidades com vínculo confirmado. O
  login antigo entra quando o SEU cookie renova (adota a própria identidade, mesmo id de linha). Até lá, token
  e cookie antigos continuam valendo (`_authenticate_patient` aceita a linha pré-conta presa à identidade dela).
- **Códigos emitidos pelo brain-api anterior** (tabela antiga) não valem depois do deploy: quem estiver com um
  código na mão nos ≤ 10 minutos do corte recebe "Código inválido ou expirado" e pede outro. Aceito (§6.26).

**Ordem de deploy:** `alembic upgrade head` (console do EasyPanel, como a 0018/0019) → brain-api → prova por
HTTP (`request-otp` só com e-mail responde 200) → frontend novo → remoção da transição (§10).

**Rollback:** reverter só o código ANTES de novos logins é limpo. Depois, linhas de login só-de-conta
(`patient_id` NULL) não são lidas pelo código antigo — quem as usa faz login de novo. O `downgrade` da 0020
exige o brain-api novo PARADO, apaga essas linhas e **descarta os códigos de convite** (um upgrade seguinte
cunha outros; links já entregues morrem). Identidades, consentimentos e as demais sessões ficam.

## §6 — Decisões tomadas sem consulta

1. **Modelo (a)** — §2.
2. **Desafio OTP em tabela nova por e-mail**, a antiga intocada: mudar a unique da antiga quebraria o código
   anterior na janela (segundo login do mesmo e-mail em outra clínica = violação de unique = 500).
3. **Sem linha de sessão por clínica.** Todo token de clínica aponta `sid` = `login_sid` = linha de login;
   `_authenticate_patient` exige que a identidade seja da conta da linha. Acaba o acúmulo de linhas de 30 min
   por refresh (pendência antiga) e o logout sem Bearer passa a derrubar todas as clínicas daquele login.
4. **Regra de pertença dos dados antigos = vínculo confirmado, ou o próprio login antigo em uso.** A primeira
   versão usava "tem consentimento `channel_access` ou alguma sessão" — e a revisão de segurança mostrou que
   TODA identidade antiga tem os dois (o login antigo gravava o consentimento com cada código): a regra juntava
   na conta toda clínica que o endereço já tocou, inclusive uma que a paciente viu como pergunta e recusou (o
   "não" nunca foi gravado). O teste que "provava" a regra usava uma linha que produção não tem. Regra atual:
   a 0020 adota só `brain_message_account_link` (essas clínicas já voltavam em todo login do endereço); um login
   antigo adota a própria identidade quando o SEU cookie renova. Cada navegador reabre exatamente o que reabria.
   Consequência inevitável, dita: quando dois aparelhos antigos do mesmo endereço forem usados, as duas
   clínicas ficam na mesma conta e os dois aparelhos veem as duas — "a conta é o e-mail" (decisão do dono).
5. **Convite inválido no `verify-otp` não falha o login** (`invited_tenant_id: null`): queimar um código certo
   por causa de um link velho seria pior, e a resposta não diz mais do que o `404` de `/clinics`.
6. **`invite` e `tenant_id` juntos → 422** (duas fontes para a mesma decisão).
7. **Confirm mantido só como reabertura** (§5), em vez de 410: com 410 o front antigo perderia as outras
   clínicas até o próximo reload.
8. **Clínica com canal desligado sai de `clinics`** e volta quando religar (a pertença fica); o 403 continua
   nas rotas de thread.
9. **Código curto:** 8 símbolos de `23456789ABCDEFGHJKMNPQRSTVWXYZ` (sem 0/O, 1/I/L, U) ≈ 6,6 × 10¹¹;
   armazenado sem hífen; entrada aceita caixa, espaço e hífen. Coluna anulável + default no ORM + backfill +
   cunhagem na leitura do staff: "alargar antes, estreitar depois" (um `NOT NULL` quebraria a criação de tenant
   pelo código anterior na janela).
10. **Onde o staff lê:** rota nova `GET /entitlements/patient-invite`, não campo novo em `EntitlementOut` (esse
    contrato é lido por frontends e pela malha; rota nova não mexe em ninguém).
11. **Link:** `BRAIN_MESSAGE_PORTAL_URL` + `/clinicas/?convite=<código>` — nome do parâmetro decidido aqui.
12. **Limite do convite:** reusa `PATIENT_LINK_RATE_LIMIT_PER_MIN` (nenhuma env nova), chave = id da conta,
    contado depois da autenticação.
13. **Mensagem única do `request-otp`** trocou de texto para não citar clínica (continua uma só).
14. **Logs:** nenhum id de conta, e-mail, nome de clínica — nem `patient_ref` em eventos de pertença
    (`patient_clinic_added` = tenant + contagem; adoção = contagem).
15. **`PATIENT_LINK_CONFIRM_WINDOW_MINUTES`** fica declarada e não lida (env implantada inofensiva).
16. **Testes do contrato de descoberta apagados (8)** e substituídos pelos do contrato novo (§9) — mudança de
    contrato de propósito, não teste afrouxado.
17. **Apagar um tenant** (cascata existente) derruba as linhas de login fixadas naquela clínica (`patient_id`
    CASCADE): quem entrou por ela faz login de novo. Aceito — trocar a FK para `SET NULL` era cirurgia fora do
    escopo.
18. **Nenhum helper faz rollback** (revisão FastAPI #2 e o 500 da corrida achado pelos testes): conta, desafio
    e identidade são criados com `INSERT … ON CONFLICT`; o consentimento com `INSERT … SELECT … WHERE NOT
    EXISTS` sob o lock da identidade. Um rollback dentro de um helper expira as instâncias do chamador e a
    leitura seguinte vira `MissingGreenlet`.
19. **Tentativa gasta antes de comparar** (as duas revisões, alta): `UPDATE … attempts = attempts + 1 WHERE
    attempts < teto AND code_hash = <lido> AND consumed_at IS NULL`; só compara se casou. Queima por CAS.
20. **Rotação com um commit só**, depois do CAS: a adoção de uma linha antiga não commita mais dentro do
    `FOR UPDATE` (revisão FastAPI #3).
21. **Login só por e-mail fica fixado na primeira clínica** (`login_clinic`/`pin_login_clinic`), em vez de
    "a primeira por nome", que mudaria ao adicionar uma clínica que ordena antes (revisão FastAPI #8).
22. **Campos de transição marcados `deprecated` no OpenAPI** por `json_schema_extra` (o `deprecated=` do
    Pydantic avisaria em toda leitura que a própria transição faz).
23. **Cunhagem do código condicional** (`WHERE patient_invite_code IS NULL` + releitura): duas leituras do
    staff não entregam dois códigos (revisão FastAPI #5).
24. **A 0020 recusa dialeto que não seja Postgres** (o casamento por `CAST(id AS VARCHAR)` não adotaria
    ninguém em silêncio); o teste de migração recusa host que não seja local (revisão de segurança #8).
25. **Logout não cria nem adota conta**: revoga pelo endereço lido da linha autenticada.
26. **Códigos da tabela antiga não são aceitos depois do corte** (revisão FastAPI #6): janela ≤ 10 min,
    produto pré-lançamento, recuperação = pedir outro código; um fallback para a tabela antiga seria código de
    transição com superfície de ataque própria para ganhar 10 minutos.
27. **Sem orçamento diário de falhas por endereço** (sugestão da revisão de segurança #1): troca brute force
    por bloqueio da vítima (qualquer um esgotaria o dia de login de um endereço) — decisão do dono, §10.

## §7 — Riscos aceitos (ditos, não escondidos)

- **Convite sem código** (decisão do dono): quem tiver um token de conta vivo (30 min, memória da página)
  adiciona clínicas àquela conta e recebe os tokens delas — o mesmo alcance que a memória da página já tem,
  porque todos os tokens de clínica da conta moram lá. Nada fora da conta fica legível.
- **A conta é o e-mail:** caixa compartilhada ou reatribuída compartilha a conta; dois aparelhos antigos do
  mesmo endereço, quando usados, juntam as clínicas deles (§6.4).
- **Enumeração por convite:** só autenticado, por conta, limitado; um código válido precisa acertar 1 em
  ~6,6 × 10¹¹ por clínica cadastrada. O nome da clínica só volta para quem acertou um convite válido.
- **E-mail OTP não é AAL2** (NIST SP 800-63B-4 §3.1.3.1): o registro e os controles compensatórios do §7 de
  `CHECKPOINT_conta_unica_multi_clinica.md` seguem valendo; o payoff de acertar um código agora é a conta. Com
  a tentativa atômica, a ordem de grandeza documentada lá (15 palpites/min por endereço ≈ 2%/dia, se os baldes
  por IP forem contornados) volta a ser o teto real.
- **Oráculo fraco de tempo** no verify (desafio vivo faz um UPDATE a mais) e **tentativas da vítima
  queimáveis** por terceiros — mesma forma do login por clínica (revisão de segurança #6).
- **Corte e rollback:** códigos em trânsito no corte morrem (§6.26); downgrade depois de códigos entregues
  mata os links das clínicas (§5).

## §8 — Revisões (`ecc:security-reviewer` e `ecc:fastapi-reviewer`, Opus, diff inteiro)

Revisão de segurança:

| # | Achado | Sev. | Veredito | Tratamento |
|---|---|---|---|---|
| 1 | Teto de 5 tentativas não atômico: `attempts += 1` lido em Python; rajada paralela sobrescreve a contagem | Alta | CONFIRMED | **Corrigido** (§6.19). Provas: teste SQLite, teste de concorrência real no Postgres e mutação antes/depois (§9). Orçamento diário por endereço: não feito (§6.27). |
| 2 | Limitadores por IP confiam no 1º `X-Forwarded-For` | Média | PLAUSIBLE | Pré-existente, fora do escopo (§10). Com o #1 corrigido, o teto por desafio vale independente do IP. |
| 3 | `rotate_patient_session` guarda um só hash anterior: ladrão que renova 2× deixa o cookie da vítima "desconhecido" (401 sem revogar) | Média | CONFIRMED (da 0019) | Pré-existente; o refresh está provado em produção e o prompt pede para não mexer no contrato — **não alterado**, pendência com a correção sugerida (id da linha no cookie) em §10. |
| 4 | Regra de adoção pegava toda identidade antiga (todas têm `channel_access`) | Média | CONFIRMED | **Corrigido** (§6.4), com fixtures iguais às de produção. |
| 5 | Primeiro `request-otp` duplo de um endereço novo → 500 na unique | Baixa | CONFIRMED | **Corrigido**: `ON CONFLICT (email) DO UPDATE`. `hide_parameters=True` → §10. |
| 6 | Oráculo de tempo; tentativas da vítima queimáveis; desafios guardados para sempre | Baixa | PLAUSIBLE | Aceito (§7); varredura → §10. |
| 7 | `patient_clinic_added` logava `patient_ref` | Baixa | CONFIRMED | **Corrigido**; o teste confere os campos exatos. |
| 8 | Teste de migração faz `downgrade base` em qualquer URL | Baixa | CONFIRMED | **Corrigido**: recusa host não local. |
| 9 | PreCheck `verify_brain_jwt` ignora `scope` | Info | CONFIRMED (outro repo) | Sem exposição hoje (rotas exigem papel). PreCheck não editado → §10. |

Revisão FastAPI:

| # | Achado | Sev. | Veredito | Tratamento |
|---|---|---|---|---|
| 1 | = segurança #1 (reproduzido: 19 palpites → `attempts=1`, código certo aceito) | Alta | CONFIRMED | Corrigido. |
| 2 | `open_account` perde a corrida → rollback expira `legacy_clinic` → `MissingGreenlet` (500, código já queimado) | Média | CONFIRMED | **Corrigido na raiz** (§6.18); `test_an_account_created_mid_login_does_not_break_the_old_body`. |
| 3 | Commit dentro do `FOR UPDATE` da rotação; deadlock entre dois aparelhos antigos | Baixa | PLAUSIBLE | **Corrigido** (§6.20); a adoção agora só toca a própria identidade (+ vinculadas). |
| 4 | = segurança #5 | Baixa | CONFIRMED | Corrigido. |
| 5 | Duas leituras do staff cunham dois códigos | Baixa | CONFIRMED | **Corrigido** (§6.23); teste com duas leituras em paralelo. |
| 6 | Códigos do deploy anterior inválidos após o corte | Baixa | CONFIRMED | Aceito e documentado (§5, §6.26). |
| 7 | (a) downgrade apaga códigos; (b) SQL só Postgres; (c) downgrade falha com o app novo gravando | Baixa | CONFIRMED | (a)(c) no docstring da 0020 e §5; (b) **corrigido** (§6.24). |
| 8 | Campos de transição sem `deprecated`; topo de login só-e-mail mutável | Info | CONFIRMED | **Corrigido** (§6.21, §6.22). |
| 9 | Lacunas: corrida do `open_account`, palpites paralelos, fixture realista, Postgres só com env | Média | CONFIRMED | **Corrigido** com os testes do §9; o módulo de Postgres continua pedindo `BRAIN_MIGRATION_PG_URL` (o repo não tem CI). |

## §9 — Provas

Tudo rodado em `C:\TECH\BRAIN\brain-api`, no código final (saídas coladas no transcript da sessão de
2026-09-15).

**Suíte completa:** `BRAIN_MIGRATION_PG_URL=postgresql+asyncpg://postgres:proof@localhost:55435/brain_proof
uv run python -m pytest -v` → `3 failed, 635 passed, 12 warnings in 450.00s (0:07:29)`, nenhum skip (o módulo
de Postgres rodou). As 3 falhas são as MESMAS de ambiente registradas em `CHECKPOINT_patient_session_refresh.md`
§6 (`test_checkout_without_stripe_key_returns_503`, `test_get_onboarding_shape_default_state`,
`test_precheck_internal_usage_event_key_unset_403` — chave Stripe, app da Meta e token do PreCheck presentes
nesta máquina); antes desta rodada: `3 failed, 608 passed`. Os 12 warnings são os pré-existentes de
`HTTP_422_UNPROCESSABLE_ENTITY`.

**Módulos da feature (SQLite):** `uv run python -m pytest tests/test_patient_access.py
tests/test_patient_account_invites.py -q` → `90 passed in 129.11s`.

**Postgres real** — container descartável `postgres:16` na porta 55435; `DATABASE_URL` só por variável de
ambiente, nenhum `.env` tocado:

- `BRAIN_MIGRATION_PG_URL=postgresql+asyncpg://postgres:proof@localhost:55435/brain_proof uv run python -m
  pytest tests/test_migration_0020_patient_accounts.py -v` →
  `test_0020_keeps_every_handle_and_what_an_old_cookie_reopens PASSED`,
  `test_the_postgres_only_statements_hold_under_real_concurrency PASSED`, `2 passed in 9.32s`.
- `alembic downgrade -1` → `0019_patient_session_rotation` (tabelas de conta: 0) → `alembic upgrade head` →
  `0020_patient_accounts (head)`. Backfill conferido por `psql` sobre linhas semeadas como o deploy anterior as
  deixa (TODAS com `channel_access`): só `Clinica linked` e `Clinica off` (vínculo confirmado) ficam
  `na_conta = t`; `login`, `separate` e `dead` ficam `f` até o cookie delas renovar.
- Mutação (script fora do repo, `scratchpad/mutation_attempts_pg.py`), 15 palpites errados em paralelo, em
  conexões separadas:
  `ANTES (attempts += 1 lido em Python): … attempts=1; código certo depois aceito=True` /
  `DEPOIS (UPDATE condicional antes de comparar): … attempts=5; código certo depois aceito=False`.
- `alembic check` depois do upgrade: nenhuma diferença nas tabelas/colunas novas. As listadas são
  PRÉ-EXISTENTES: `created_at` anulável nas tabelas da 0018 (o model declara `NOT NULL`), a unique de
  `message_patient_sessions.token_hash` (constraint na 0018, índice único no model) e o índice de
  `precheck_topup_credits` (já registrado em `CHECKPOINT_brain_message_canal.md`).

**`uv run ruff check`** nos 16 arquivos tocados → `All checks passed!` (`ruff format` NÃO rodado — convenção
do repo).

Achado pelos testes durante a execução, antes das revisões: a corrida de duas adições da mesma clínica nova
virava `MissingGreenlet` (500) pelo rollback dentro de `add_clinic` — primeiro remendado recarregando as
instâncias, depois eliminado na raiz com `ON CONFLICT` (§6.18).

Testes por item do checklist do prompt (§5):

| Item | Teste(s) |
|---|---|
| e-mail só abre a conta; corpo antigo = login + convite | `test_email_alone_opens_an_account_with_no_clinic`, `test_the_old_body_is_login_plus_invite_of_that_clinic`, `test_request_otp_never_reveals_whether_the_clinic_is_reachable` |
| conta nova + `invite` no verify | `test_a_first_contact_through_the_clinic_link_lands_with_that_clinic` |
| conta aberta + convite: sem código, idempotente, 1 consentimento | `test_an_open_account_adds_a_clinic_without_a_code_and_only_once`, `test_an_invite_on_an_old_login_needs_no_code`, `test_parallel_adds_of_a_new_clinic_make_one_identity_and_one_consent` |
| UUID, código e link = mesma clínica; recusa única | `test_uuid_code_and_links_name_the_same_clinic[uuid,code,typed_code,link,old_link]`, `test_every_bad_invite_gets_the_same_refusal[5]`, `test_the_parser_never_guesses` |
| só casa pelo e-mail → fora da conta e do refresh | `test_a_clinic_that_only_shares_the_address_stays_out_until_invited`, `test_a_clinic_logged_into_separately_stays_out_until_its_own_session_returns`, `test_confirm_without_a_matching_candidate_is_refused[not_in_account]` |
| ids não mudam; conta antiga reabre as mesmas clínicas | `test_0020_keeps_every_handle_and_what_an_old_cookie_reopens` (Postgres), `test_a_confirmed_link_from_before_rides_the_first_refresh` |
| refresh/graça/90 dias/logout | seção 8 de `tests/test_patient_access.py` (ajustados só em `linked_sessions` e na contagem de linhas), `test_logout_*` de `tests/test_patient_access.py`, `test_logout_with_the_account_token_ends_every_clinic`, `test_an_invite_that_races_a_logout_dies_with_the_login` |
| achados das revisões | `test_parallel_wrong_guesses_cannot_outrun_the_attempts_ceiling`, `test_two_requests_with_the_right_code_open_the_account_once`, `test_an_account_created_mid_login_does_not_break_the_old_body`, `test_an_email_only_login_keeps_the_clinic_it_was_pinned_to`, `test_the_staff_reads_its_own_invite`, `test_account_logs_carry_no_address_no_clinic_name_no_account_id_no_handle`, `test_the_postgres_only_statements_hold_under_real_concurrency` |

Apagados de propósito (contrato de descoberta que saiu): `test_verify_otp_names_a_sibling_clinic_without_its_token`,
`test_confirm_opens_the_existing_identity_and_records_the_link_once`, `test_a_link_that_races_a_logout_dies_with_the_login`,
`test_confirm_needs_a_recent_login`, `test_discovery_and_linking_log_tenants_and_counts_only`,
`test_a_linked_clinic_survives_a_refresh`, `test_refresh_never_mints_a_session_for_a_clinic_that_was_only_discovered`,
`test_a_first_link_on_an_old_session_still_needs_a_fresh_code` — cada propriedade que ainda vale ganhou teste
novo acima.

## §10 — Pendências

- [ ] `alembic upgrade head` em produção (0020) → deploy do brain-api → prova por HTTP → prompt irmão do front.
- [ ] Setar `BRAIN_MESSAGE_PORTAL_URL` no EasyPanel (não secreta) para `invite_link` aparecer.
- [ ] **Onde a clínica VÊ o código e o link numa tela** (secretarIA-frontend ou brain-frontend) — fora deste
      prompt; a rota `GET /entitlements/patient-invite` já existe.
- [ ] Depois do front novo no ar: remover `tenant_id` dos corpos de login, os campos de transição, a rota
      `/siblings/*`, `PATIENT_LINK_CONFIRM_WINDOW_MINUTES`, e dropar `message_patient_otps` numa migração nova.
- [ ] Decisão do dono: orçamento diário de falhas por endereço (brute force × bloqueio da vítima).
- [ ] Pré-existentes, recomendados: id da linha no cookie para detectar reuso depois de duas rotações (revisão
      de segurança #3); `client_ip` confiável para os limitadores por IP; `hide_parameters=True` no engine;
      varredura de sessões e desafios vencidos; PreCheck `verify_brain_jwt` recusar token com `scope`; drift do
      `alembic check` na 0018 (`created_at` anulável, unique de `token_hash`).
