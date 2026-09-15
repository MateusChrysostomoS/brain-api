# brain-api

## Documentação — manter em dia (obrigatório)

Os arquivos em `docs/` são a **fonte de verdade pra entender o projeto** — o objetivo é que uma
sessão nova do Claude Code (ou qualquer pessoa) entenda tudo, profundamente, só lendo `docs/`.
Por isso eles **têm que refletir o estado real** do projeto.

**Quando atualizar:** ao fazer mudanças numa sessão, atualize os docs afetados — **não
necessariamente na hora de cada mudança, mas no FIM da sessão**, depois que tudo foi **validado e
verificado** (testes passando, deploy/migração confirmados). Documentar antes de validar gera doc
errado; documentar depois garante que o doc descreve o que realmente está no ar.

**Regras:**
- Feature grande/multi-camada → um `docs/CHECKPOINT_<FEATURE>.md` (estado, o que entrou onde,
  deployado/testado, pendências) + 1 linha de ponteiro nos docs relevantes.
- Cite âncoras estáveis (nome de função/módulo), não números de linha frágeis, quando possível.
- Mantenha o `CHECKPOINT_*` da feature em dia até ela ser 100% concluída/encerrada; aí vira histórico.

## Prompts pendentes

- `z_prompts/PROMPT_SECRETARIA_CONFIG_PROFISSIONAIS_NAO_CARREGA.md` (raiz de BRAIN, gerado
  2026-09-12) — bug ao vivo: aba "Profissionais" de `/configuracao` no `secretarIA-frontend`
  não carrega dados de clínica real nenhum. Duas das pistas levantadas tocam este repo: CORS
  allowlist (`46b4eb3`, `src/brain_api/config.py`) e a migração `0012_role_taxonomy`
  (`docs/CHECKPOINT_role_taxonomy_admin_tabs.md`, ainda marcada "NOT yet run in production"
  em 2026-08-07, sem confirmação mais recente) — nenhuma confirmada ainda, exige reproduzir
  ao vivo primeiro.
- `z_prompts/PROMPT_BRAIN_MESSAGE_CANAL_ENTITLEMENT.md`,
  `..._OTP_SWITCHBOARD.md` (raiz de BRAIN, convenção compartilhada, gerados 2026-09-07) —
  trazem secretarIA e PreCheck pra funcionar também pelo canal Brain-Message: campo de canal
  (`whatsapp`/`brain_message`) no Tenant/EntitlementOut, e o switchboard de acesso do
  paciente por OTP e-mail (decisão já tomada com o dono) que roteia pra secretarIA e/ou
  PreCheck pela malha `/internal` existente. Parte de um conjunto de 10 prompts cross-repo —
  o segundo depende do primeiro e dos endpoints internos criados em secretarIA e PreCheck
  (`PROMPT_BRAIN_MESSAGE_SECRETARIA_PIPELINE_CANAL.md`,
  `PROMPT_BRAIN_MESSAGE_PRECHECK_CONDUTOR.md`).
  **`..._CANAL_ENTITLEMENT.md` EXECUTADO em 2026-09-08** (canal no `Tenant` +
  `EntitlementOut.channels`, migração `0017_message_channels`) — estado, provas e as
  4 decisões tomadas sem consulta estão em `docs/CHECKPOINT_brain_message_canal.md`.
  **`..._OTP_SWITCHBOARD.md` EXECUTADO em 2026-09-08, COMMITADO** (`795cc26`, `/patient-access/*`
  + migração `0018_patient_access`) — a migração não estava aplicada em produção (500 em
  `request-otp`), corrigido 2026-09-09 via `alembic upgrade head` manual no console EasyPanel.
  `z_prompts/PROMPT_BRAIN_MESSAGE_E2E_QA_PRODUCAO.md` (raiz de BRAIN, gerado 2026-09-09) é o
  roteiro de QA ao vivo pra essa cadeia inteira (staff + portal do paciente), com um bug ainda
  não corrigido: e-mail do OTP nunca chega porque a secretarIA não tem o template
  `patient_access_otp` cadastrado.
- `z_prompts/PROMPT_BRAIN_MESSAGE_PORTAL_INTERACTIVE_TAP.md` (raiz de BRAIN, gerado 2026-09-11,
  com autorização explícita do dono pra tocar este repo) — toque real em botões/listas no Portal
  do paciente (`/conversa`), continuação de `PROMPT_BRAIN_MESSAGE_INTERACTIVE_BUBBLES_RENDERING.md`
  (que só cobriu o console de staff). `PatientMessageIn` (`schemas/patient_access.py`,
  `extra="forbid"`) e `message_switchboard.py::send_message` ganham um campo novo
  (`interactive_reply_id`) só no ramo secretarIA — PreCheck fica intocado. **NÃO EXECUTADO
  ainda.** Confirmar com o dono antes de qualquer deploy real aqui (ver §0 do prompt).
- `z_prompts/PROMPT_BRAIN_MESSAGE_PORTAL_MULTI_CLINICA_CONTA_SEGURA.md` (raiz de BRAIN, gerado
  2026-09-12 via `/prompt-generator`, com pesquisa de práticas de segurança embutida) — o dono
  pediu conta única multi-clínica pro paciente (hoje `MessagePatient` isola identidade por
  `(tenant_id, email)` de propósito, ver o docstring do modelo). Descoberta de clínicas-irmãs
  por e-mail (`find_sibling_candidates`) + confirmação explícita nomeada (nunca vínculo
  silencioso por e-mail sozinho, padrão OWASP de account-linking) antes de mintar qualquer
  sessão nova; logout revoga a conta inteira; nota de risco aceito sobre e-mail OTP não
  atender NIST SP 800-63B Rev. 4 AAL2. Sem migração. Roda nesta mesma branch
  (`feature/whatsapp-patient-vision`, decisão do dono em 2026-09-12 — não abrir branch nova
  aqui). Depende do frontend (`PROMPT_BRAIN_MESSAGE_PORTAL_MULTI_CLINICA_FRONTEND.md`,
  Brain-Message-Frontend) rodar depois. **EXECUTADO em 2026-09-12, commitado (`54bffe4`)
  em 2026-09-14 junto com o merge de `feature/whatsapp-patient-vision` pra `main` — ainda não
  deployado** (sem migração): estado, contrato HTTP pro frontend, as decisões tomadas sem
  consulta — incluindo dois desvios do texto (o JWT do paciente ganhou `sid`/`login_sid`
  conferidos em `get_current_patient`, e confirmar exige o cookie do próprio login + login
  recente) —, a revisão de segurança com o tratamento de cada achado e a nota de risco
  aceito NIST SP 800-63B-4 estão em `docs/CHECKPOINT_conta_unica_multi_clinica.md`. Padrão
  registrado como skill: `TECH/.claude/skills/cross-tenant-account-linking/`. O `pytest` não
  precisa de Docker/Postgres local — a suíte usa SQLite em memória (`tests/conftest.py`).
- `z_prompts/PROMPT_BRAIN_MESSAGE_PATIENT_SESSION_REFRESH_BACKEND.md` (raiz de BRAIN, gerado
  2026-09-14 via `/prompt-generator`) — fecha a lacuna que a conta única multi-clínica acima
  deixou aberta: `__Host-patient_session` existe (`verify_otp`) mas nunca é lido pra emitir
  um access token novo (`PATIENT_TOKEN_EXPIRE_MINUTES=30`, sem rota de refresh —
  `config.py:359-361` já admite isso no comentário). Pede `POST /patient-access/refresh`
  espelhando `api/auth.py::refresh`/`rotate_refresh_token` (padrão já documentado em
  `TECH/.claude/skills/auth-jwt-multitenant/`), teto de sessão subindo de 30 pra **90 dias**
  deslizantes (decisão do dono, 2026-09-14), e resolvendo três riscos concretos achados na
  investigação: girar o `id` da sessão de login invalidaria `login_sid` de clínicas-irmãs já
  vinculadas; polling concorrente pode disparar a detecção de reuso do rotate-on-use e
  derrubar a conta inteira; `session_is_recent`/`created_at` (usado por `confirm_sibling`)
  fica preso aos 30 minutos do login original se a sessão renovar indefinidamente. Depende
  do frontend (`..._FRONTEND.md`, Brain-Message-Frontend) rodar DEPOIS e só contra produção
  deployada. **EXECUTADO em 2026-09-14, commit `c6cede8` — migração
  `0019_patient_session_rotation` NÃO aplicada em produção, não deployado.** `POST
  /patient-access/refresh` renova a linha de login IN PLACE (id estável, só o valor gira, teto de
  90 dias deslizante), reemite as clínicas já vinculadas (`linked_sessions`), aceita o cookie
  anterior por 60s de graça (compare-and-swap; fora da janela = reuso → conta revogada), e
  `created_at` NÃO desliza (vincular clínica nova numa sessão antiga segue pedindo código).
  Estado, contrato HTTP pro frontend, as três escolhas dos riscos e as decisões sem consulta em
  `docs/CHECKPOINT_patient_session_refresh.md`. Skill `auth-jwt-multitenant` estendida com a
  seção "The patient population". **Ordem de deploy: `alembic upgrade head` → brain-api → só
  então o prompt irmão do frontend.** **Atualização 2026-09-14 (noite): deployado e PROVADO em
  produção** com contas reais (F5, aba fechada/reaberta, 2 clínicas, renovação automática com a aba
  aberta) — ver a memória/relatório "Auditoria do Portal em Produção".
- `z_prompts/PROMPT_BRAIN_MESSAGE_PORTAL_CLINICAS_BACKEND.md` (raiz de BRAIN, gerado 2026-09-14 via
  `/prompt-generator`, com decisões fechadas com o dono) — **reverte parte da conta multi-clínica**:
  o login passa a ser **só por e-mail** (hoje `issue_otp` exige `tenant_id`), a descoberta de
  clínicas-irmãs por e-mail + `POST /siblings/{tenant_id}/confirm` SAI, e uma clínica só entra na conta
  por convite (link da clínica, ou link/código curto colado pela paciente), sem pedir código de novo
  para quem já tem conta. Inclui `tenants.patient_invite_code`, compatibilidade com o frontend atual
  (corpo antigo com `tenant_id` = login + convite) e a restrição de nunca mudar `MessagePatient.id`
  (handle da secretarIA e do PreCheck). Resolve o bug de prioridade média da auditoria (link de outra
  clínica ignorado com conta aberta). **EXECUTADO em 2026-09-15, UNCOMMITTED (sem hash: commit é
  decisão do dono) e NÃO deployado — migração `0020_patient_accounts` NÃO aplicada em produção.** Conta =
  `message_patient_accounts` (e-mail único) + `MessagePatient.account_id`; token de conta
  (`scope=patient_account`) + um token por clínica ligado à linha de login; `POST /patient-access/clinics`
  (convite por UUID, código curto de 8 ou link); `GET /entitlements/patient-invite` (staff); corpo antigo
  com `tenant_id` segue valendo na janela. Estado, contrato HTTP com exemplos, compatibilidade, decisões
  sem consulta, revisão e provas (migração provada em `postgres:16` descartável) em
  `docs/CHECKPOINT_portal_clinicas_convite.md`; skill `cross-tenant-account-linking` reescrita para o
  modelo de convite. Deploy: `alembic upgrade head` → brain-api → só então o irmão
  `PROMPT_BRAIN_MESSAGE_PORTAL_CLINICAS_FRONTEND.md`.
- `z_prompts/PROMPT_BRAIN_MESSAGE_PRECHECK_PARIDADE_4_EXAMES_BRAIN_API.md` (raiz de BRAIN, gerado 2026-09-14;
  **Opus 5, esforço alto**) — parte 4 da série de paridade do PreCheck no Portal: `PatientMessageIn`
  (`extra="forbid"`) passa a aceitar anexo só no produto precheck, com validação de tipo real/tamanho antes de
  repassar ao PreCheck, e uma rota autenticada para a mídia do transcript chegar ao Portal sem afrouxar a CSP.
  Depende da parte 3 (PreCheck) deployada; depois vem a parte 5 (frontend). **NÃO EXECUTADO.**

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
