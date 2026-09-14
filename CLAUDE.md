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
  Brain-Message-Frontend) rodar depois. **EXECUTADO em 2026-09-12, UNCOMMITTED, não
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
  deployada. **NÃO EXECUTADO ainda.**

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
