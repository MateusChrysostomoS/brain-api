# CHECKPOINT — papel `secretary` (secretária humana da clínica)

**Rodada:** 2026-08-14 · **Estado:** BUILT + testado local (441 testes brain-api verdes,
116 vitest brain-frontend verdes, build estático limpo) · **NÃO commitado, NÃO deployado,
migração 0013 NÃO rodada em produção.**

Repos tocados: `brain-api` e `brain-frontend`. `secretarIA` (repo) e `PreCheck` **não foram
tocados** — só lidos.

---

## 1. O que é o papel

4º papel ao lado de `admin`/`doctor`/`manager`: `secretary` — a **secretária HUMANA** da
clínica (recepção/operação), não o bot secretarIA. Decisões de produto (fechadas, não
reabrir sem decisão nova):

- **Escopo: só secretarIA.** Nunca alcança dado clínico / PreCheck.
- **Poder total dentro da secretarIA:** agenda de todos os profissionais, configuração
  inteira, gestão de equipe (convidar médicos **e** outras secretárias), billing/assinatura,
  **e** pausar retentativa de conexão/lembretes de config.
- **Nunca é profissional:** `professional_id` fica `NULL` pra sempre, então uma secretária
  jamais aparece na agenda como alguém que pacientes marcam.

Constante: `ROLE_SECRETARY = "secretary"` (`models/user.py`), dentro de `ROLES`.

## 2. Migração 0013 é um NO-OP deliberado

`users.role` é `sa.String(length=32)` puro — **sem enum nativo, sem check constraint** em
nenhum ponto do histórico de migrações (conferido em `0001_initial_schema.py`). Papel é
validado só na camada de aplicação (`models/user.ROLES`, tuplas de `api/deps.py`, o
`Literal[...]` de `schemas/admin.AdminUserCreateIn`). Logo:

- não há DDL a rodar pra aceitar uma string de papel nova;
- não há backfill (nenhuma linha existente vira `secretary` retroativamente — 0012 só
  reescreveu linhas porque **renomeou** dois valores).

`0013_secretary_role.py` existe com `upgrade()`/`downgrade()` vazios e um docstring
explicando isso, pra que a mudança de taxonomia apareça no histórico onde alguém que lê a
0012 vai procurar, e pro checklist de deploy ter um "nada a rodar" explícito em vez de um
silêncio ambíguo. Rodar é seguro e instantâneo; pular não muda nada.

> Se uma rodada futura adicionar constraint de banco em `users.role`, é **aquela** migração
> que precisa incluir `secretary`.

## 3. Gates — o papel é uma AMPLIAÇÃO, então a fronteira é explícita

`api/deps.py`:

- `DOCTOR_ROLES = (ROLE_DOCTOR, ROLE_MANAGER, ROLE_SECRETARY)` → `secretary` passa em
  `require_doctor` (que agora é documentado como o gate do **portal operacional
  tenant-scoped**, não um gate clínico — ele guarda o espaço de URL `/doctor/*`).
- `require_owner` aceita `p.role == ROLE_SECRETARY` como **alternativa** a `p.is_owner`.
  ⚠️ Consequência registrada na própria docstring: **qualquer ação owner-only criada no
  futuro nasce aberta pra secretary.** Quem não quiser isso deve gatear em `p.is_owner`
  direto em vez de usar `require_owner`.
- `deny_secretary(principal, error_code)` — guard simples (não é dependency FastAPI, de
  propósito: cada call site nomeia seu próprio código de erro e
  `grep -rn deny_secretary src` enumera a fronteira inteira num comando só).

### Sweep completo de `require_owner`/`require_doctor`

`require_owner` tem **exatamente um** consumidor hoje: `POST /doctor/onboarding/pause`.

| rota | gate | secretary? | ação |
|---|---|---|---|
| `GET/PATCH /doctor/me` | `require_doctor` | ✅ operacional | — |
| `GET /doctor/appointments` · `/patients` | `require_doctor` | ✅ agenda | — |
| `POST /doctor/secretaria/hub-token` | `require_doctor` | ✅ core | — |
| `GET /doctor/anamneses` · `/{id}` | `require_doctor` | ❌ **clínico** | `deny_secretary` |
| `GET /doctor/onboarding` · `POST /attempt` · `/resolve-blocker` · `/intake` | `require_doctor` | ✅ | — |
| `POST /doctor/onboarding/pause` | `require_owner` | ✅ **decisão explícita** | `require_owner` ampliado |
| `GET /doctor/professionals` · `POST /professionals/invites` | `require_doctor` | ✅ gestão de equipe | — |
| `POST /doctor/professionals/self` | `require_doctor` | ❌ **viraria profissional** | `deny_secretary` |
| `GET /doctor/onboarding/test-window` · `POST /restart` | `require_doctor` | ✅ | — |
| `POST /sso/precheck/token` | `require_tenant` | ❌ **PreCheck** | `deny_secretary` |
| `billing.py` (5 rotas) · `GET /entitlements` | `require_tenant` | ✅ billing | — (já passava) |

Confirmado lendo o corpo de `require_tenant`: ela só checa `tenant_id is not None`, sem
restrição de papel — por isso billing e entitlements ficaram acessíveis **de graça**, sem
mudança em `billing.py`/`entitlements.py`.

`admin.py`/`privacy.py` (`require_role("admin")`) e `internal*.py` (chave de API) não foram
tocados — não batem com `secretary` por construção.

### Os 4 pontos de exclusão

| rota | código | por quê |
|---|---|---|
| `POST /sso/precheck/token` | `secretary_precheck_not_allowed` | minta sessão do PreCheck |
| `GET /doctor/anamneses` | `secretary_precheck_not_allowed` | prontuário, proxy do PreCheck |
| `GET /doctor/anamneses/{id}` | `secretary_precheck_not_allowed` | idem |
| `POST /doctor/professionals/self` | `secretary_cannot_be_professional` | única rota que **escreve** `professional_id` |

**As anamneses e o self-bind não estavam no prompt original desta rodada** — apareceram no
sweep. Ambos seguem direto da decisão de produto já fechada ("sem dado do PreCheck" / "sem
`professional_id`"), então foram implementados em vez de virarem pergunta. Se a intenção
era outra, são 2 linhas de `deny_secretary` a remover.

PreCheck tem defesa em profundidade própria: `BRAIN_DOCTOR_ROLES`
(`app/core/brain_auth.py`) não lista `secretary`, então o token repassado seria recusado lá
também — mas isso é um 403 remoto virando erro opaco de upstream, e por isso o gate local
existe.

## 4. Endpoints novos (`api/onboarding.py`)

- `GET /doctor/secretaries` → `{items: [{user_id, name, email, invite_pending, created_at}]}`.
  Puramente LOCAL: secretária não tem linha em `professionals` da secretarIA, então não há
  config-status pra juntar nem completude pra reportar.
- `POST /doctor/secretaries/invites` → body `{name, email}` (sem `specialty`),
  `201 {user_id, invite_link}`, `409 email_already_registered`.

Espelha `invite_professional` **exceto** no que define o papel: **não chama
`secretaria_provisioning.create_professional`**. Nenhuma linha em `professionals`,
`professional_id=None` — e por consequência **não existe branch de 502** (nenhum serviço
irmão é tocado antes do commit).

Schemas próprios (`SecretaryOut`/`SecretariesOut`/`SecretaryInviteIn`/`SecretaryInviteOut`)
em vez de reusar os `Professional*`: todo campo daqueles seria `None` permanente aqui.

### E-mail: template `professional_invite` REUTILIZADO de propósito

Decisão fechada. A copy já é genérica de equipe ("Você foi adicionado(a) à equipe da
{clinic_name}…", `secretarIA/src/secretaria/services/email.py`) — não fala "médico" nem
"profissional" em lugar nenhum. Criar template novo exigiria editar a secretarIA (repo
só-leitura nesta rodada) **e** teria um modo de falha ruim: nome de template desconhecido
faz a secretarIA logar `transactional_email_unknown_template` e retornar `False` — o
convite "funciona" (o `invite_link` volta) mas o e-mail nunca sai, silenciosamente.

Resgate do convite: `POST /auth/exchange-invite-token` é **role-agnóstico** (resolve o
usuário pelo hash do token e minta a sessão normal dele), então funciona sem nenhuma
mudança.

## 5. Sweep de `ROLE_MANAGER` (lugares que tipam/listam papéis)

- `models/user.py` — constante + `ROLES` ✅
- `api/deps.py` — import + `DOCTOR_ROLES` ✅
- `schemas/admin.py` — `Literal["admin","doctor","manager","secretary"]` + mensagem do
  validador de tenant ✅
- `services/admin.py::create_user` — `is_manager` só é forçado pra `ROLE_MANAGER`;
  `secretary` cai no ramo "pega o payload verbatim", que é o comportamento certo ✅ (sem
  mudança necessária)
- `services/admin.py::issue_impersonation_token` — a checagem de papel-alvo do "Modo médico"
  (`(ROLE_DOCTOR, ROLE_MANAGER, *_LEGACY_DOCTOR_ROLES)`) **NÃO** ganhou `secretary`:
  impersonar uma secretária não foi pedido e não é necessário (o alvo é o dono da clínica
  demo). Decisão consciente, não esquecimento.

## 6. Testes

`tests/test_secretary_role.py` — 12 testes, matriz ALCANÇA × RECUSADO:

- taxonomia (`ROLE_SECRETARY in ROLES`), login + claim `role` no JWT, `professional_id`
  ausente;
- alcança: `/doctor/me`, `/appointments`, `/patients`, `/professionals`, `/secretaries`,
  `/entitlements`, `/billing/precheck/usage`, pause;
- pause: secretary 200 **e** médico não-dono ainda 403 (regressão do `require_owner`);
- convite de secretária: sem `create_professional` (monkeypatch que explode se chamado),
  `role=secretary`, `professional_id=None`, template `professional_invite`, 409 duplicado;
- secretary convida profissional;
- ponta a ponta: convite → `exchange-invite-token` → `set-password` → login com o papel novo
  → listagem mostra `invite_pending=false`; replay do token = 401;
- recusado: SSO PreCheck (**e o médico do MESMO tenant entitled chega a 409
  `precheck_account_not_linked`**, provando que a exclusão dispara ANTES e é gate de
  verdade), anamneses (lista + detalhe; médico ainda 200), self-bind, `/admin/*`;
- admin cria secretária via `POST /admin/users` (+ 422 sem tenant).

`441 passed` no repo inteiro. `ruff check` limpo nos arquivos tocados (o repo tem dívida de
lint pré-existente em outros arquivos — 13 erros, 21 arquivos que `ruff format` reescreveria
— **não** introduzida por esta rodada).

## 7. brain-frontend

- `lib/manage-api.ts`: `Role` += `"secretary"`; `getDoctorSecretaries` + `createSecretaryInvite`
  (+ tipos `DoctorSecretary`/`SecretaryInvite*`); comentários obsoletos de "owner-only" em
  `createProfessionalInvite`/`createSelfProfessional` corrigidos.
- `usePortalGuard`: `secretary` adicionado em 6 rotas (`doctor/layout`, `dashboard`,
  `pacientes`, `perfil`, `app/onboarding`, `app/reativar`). **`doctor/anamneses` foi
  deixado de fora de propósito**, com comentário explicando por quê (senão alguém
  "conserta" e troca um redirect limpo por uma página quebrada com 403).
- `InviteProfessionalModal.tsx` → **`InviteTeamMemberModal.tsx`** (arquivo antigo removido):
  mesmo modal com prop `kind: "professional" | "secretary"` — sem toggle interno, o botão
  que abriu já escolheu. Especialidade só aparece pro profissional; a variante secretária
  ganha um parágrafo explicando o alcance do papel.
- `ProfessionalsSection.tsx`: dois botões ("Convidar profissional" / "Convidar secretária"),
  lista "SECRETÁRIAS (RECEPÇÃO)" com `SecretaryRow` (mais enxuta — sem agenda/serviços/
  horários pra completar, nada a selecionar). Busca as secretárias **no próprio componente**
  em vez de no `page.tsx`: a lista é local-only do brain-api, não precisa do gate `hubReady`
  do hub e não alimenta a máquina de estado do profissional selecionado.
- Prompt de auto-vínculo ("Você também atende pacientes?") explicitamente escondido pra
  `secretary` (backstop pro caso de uma secretária criada com `is_owner` por tooling admin).
- Rótulos/badges de papel em `admin/users` e `doctor/perfil` + `<option>` "Secretária" no
  formulário de criação do admin.
- **Billing não precisou de mudança**: `/app/billing` só exige sessão, não tem checagem de
  papel nem de `is_owner` client-side — o botão "Gerenciar assinatura" já aparece pra
  secretary.
- 4 testes novos em `lib/__tests__/manage-api.test.ts` (116 no total). `tsc.cmd --noEmit`
  limpo, `npm run build` (com `C:` maiúsculo) limpo.

## 8. secretarIA-frontend

Não replicado: em 2026-08-14 o repo `C:\TECH\BRAIN\secretarIA-frontend` está **vazio**
(só `.gitignore` + commit inicial `be0494b`) — `PROMPT_FABLE_secretarIA-frontend.md` ainda
não rodou. Quando rodar, o clone de `configuracao/*` pega a UI de convite de graça; o
`usePortalGuard` de lá já foi escrito esperando `secretary`, que agora existe de verdade.

## 9. Pendências

- [ ] Commit + push nos dois repos (nada foi commitado).
- [ ] Deploy dos dois serviços.
- [ ] `alembic upgrade head` em produção — sobe 0012 (ainda pendente de rodadas anteriores)
      **e** 0013 (no-op).
- [ ] Teste ao vivo do convite ponta a ponta com e-mail real (o teste automatizado
      monkeypatcha o envio).
- [ ] Se/quando `secretarIA-frontend` for populado, replicar TAREFA 4 lá.
