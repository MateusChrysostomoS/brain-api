# brain-api

## Documentação

`docs/` é a fonte de verdade deste repo. Regra geral de quando/como atualizar (CHECKPOINT,
âncoras estáveis) em `AI_WORKFLOW.md` — aqui só o que diverge, se houver.

O contrato de integração do canal Brain-Message (envio/recebimento entre produtos, autenticação
`/internal/brain-message/*`, anexos, erros) está em `docs/PORTAL_MESSAGING_API.md` — mantenha em
dia ao mudar este lado do contrato.

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
  clínica ignorado com conta aberta). **EXECUTADO em 2026-09-15, COMMITADO em `main`
  (`bc29f76 Add comprehensive tests for patient account invites and clinic associations`, confirmado
  2026-09-16) — deploy em produção segue NÃO confirmado (migração `0020_patient_accounts`, sem forma
  de checar por código; não presuma aplicada).** Conta =
  `message_patient_accounts` (e-mail único) + `MessagePatient.account_id`; token de conta
  (`scope=patient_account`) + um token por clínica ligado à linha de login; `POST /patient-access/clinics`
  (convite por UUID, código curto de 8 ou link); `GET /entitlements/patient-invite` (staff); corpo antigo
  com `tenant_id` segue valendo na janela. Estado, contrato HTTP com exemplos, compatibilidade, decisões
  sem consulta, revisão e provas (migração provada em `postgres:16` descartável) em
  `docs/CHECKPOINT_portal_clinicas_convite.md`; skill `cross-tenant-account-linking` reescrita para o
  modelo de convite. Deploy: `alembic upgrade head` → brain-api → só então o irmão
  `PROMPT_BRAIN_MESSAGE_PORTAL_CLINICAS_FRONTEND.md` (substituído, ver `PORTAL_CHAT_SEM_GATE.md` no
  Brain-Message-Frontend).
- `z_prompts/PROMPT_BRAIN_MESSAGE_PORTAL_SESSAO_PENDENTE.md` (raiz de BRAIN, gerado 2026-09-16 via
  `/prompt-generator`, onda 1 de `PLANO_LOGIN_SEM_GATE_PACIENTE_NOVO.md`) — cria o conceito de sessão
  pendente (identidade sem e-mail verificado, para o paciente conversar e agendar antes de confirmar
  a conta) e o link direto ao PreCheck sem autenticação. Consome o modelo de conta do prompt acima
  (nunca muda `MessagePatient.id`). **EXECUTADO em 2026-09-16, COMMITADO
  (`5a2d5e6 feat(patient-access): implement pending visit flow allowing chat before email
  verification`), DEPLOYADO e PROVADO ao vivo em produção no mesmo dia** — migração `0021`
  confirmada aplicada; `/pending`, `/clinics/lookup` e o relay ponta a ponta pro PreCheck testados
  contra `https://secretaria-brain-api.cpux9k.easypanel.host` no tenant "Chrysostomo For Eyes"
  (evidência em `docs/CHECKPOINT_portal_sessao_pendente.md` §11). Não provado ainda: o round-trip
  de OTP da visita pendente, que exige a `X-Internal-Api-Key` real (não obtida do EasyPanel de
  propósito). Uma visita
  (`message_pending_sessions`) carrega a conversa antes de qualquer e-mail; a identidade nasce com
  `MessagePatient.email` anulável e **mantém o mesmo id** depois do código. Quatro rotas novas
  (`POST /patient-access/clinics/lookup`, `/pending`, `/pending/request-otp`,
  `/pending/verify-otp`) + `POST /internal/brain-message/pending-email` (a secretarIA entrega o
  e-mail pela perna de serviço — o navegador nunca nomeia a caixa que recebe o código). Terceiro
  token do paciente (`scope=patient_pending`) + cookie `__Host-patient_pending`. Link direto do
  PreCheck = `POST /pending` com `product: "precheck"`, aberto, sem gate de agendamento em lugar
  nenhum. Contrato HTTP completo, formato do token, 9 decisões sem consulta, 6 riscos aceitos,
  ordem de deploy e provas em `docs/CHECKPOINT_portal_sessao_pendente.md`; skill
  `cross-tenant-account-linking` estendida com a seção "identidade antes do atributo". **Ordem de
  deploy: `alembic upgrade head` → brain-api → só então secretarIA (onda 2) → frontend (onda 3).**
  **EMENDA 2026-09-17 PARA A ONDA 2: COMMITADA E PUSHED em `main` (`3ee89b7`); endpoints públicos
  e guardas observados em produção via Browser Act, incluindo `pending/status=200`, o que também
  prova a coluna da migração `0022`; a revisão exata do build ainda deve ser registrada no
  fechamento operacional** — migração
  aditiva `0022`, status/request/verify internos sem PII/credencial e os endpoints públicos
  `GET /patient-access/pending/status` + `POST /patient-access/pending/complete`; contrato, segurança,
  compatibilidade do `/pending/verify-otp` e provas em
  `docs/CHECKPOINT_portal_sessao_pendente.md` §12. A ordem agora é 0022 → brain-api →
  `secretaria_api`+worker → frontend.
- `z_prompts/PROMPT_BRAIN_MESSAGE_FECHAR_ONDA_2_ROLLOUT_E2E.md` (raiz de BRAIN, gerado 2026-09-17
  via `$prompt-generator`, revisado após o primeiro QA) — fechamento sequencial da onda 2. Trata
  este repo como contrato congelado já comprovado por probes públicos; a falha localizada era o gate
  WhatsApp `Tenant.is_active` herdado por `brain_message` na secretarIA. A correção está
  commitada/pushed em `secretarIA/main@b2f3055`. **EXECUTADO 2026-09-17 — ONDA 2 FECHADA.**
  `secretaria_api` e `secretaria-worker` rodam `secretarIA/main@697c24a` (`595b1f80df8c`,
  `deploy_parity=match`), e o E2E foi provado ponta a ponta na clínica QA contra ESTE repo em
  `3ee89b7`: `pending` → `pending_unclaimed` → `pending_claimed` → `otp_sent` → `verified` →
  `POST /pending/complete` uma única vez (a segunda chamada devolve `401`), com `patient_ref`
  preservado e a visita sobrevivendo a F5 via cookie `__Host-`. O contrato deste repo não mudou;
  o defeito corrigido estava na secretarIA. Ver `docs/CHECKPOINT_portal_sessao_pendente.md` §12.
- `z_prompts/PROMPT_BRAIN_MESSAGE_PRECHECK_PARIDADE_4_EXAMES_BRAIN_API.md` (raiz de BRAIN, gerado 2026-09-14;
  **Opus 5, esforço alto**) — parte 4 da série de paridade do PreCheck no Portal: `PatientMessageIn`
  (`extra="forbid"`) passa a aceitar anexo só no produto precheck, com validação de tipo real/tamanho antes de
  repassar ao PreCheck, e uma rota autenticada para a mídia do transcript chegar ao Portal sem afrouxar a CSP.
  Depende da parte 3 (PreCheck) deployada; depois vem a parte 5 (frontend). **NÃO EXECUTADO.**
- `z_prompts/PLANO_PORTAL_API_MVP.md` (raiz de BRAIN, gerado 2026-09-17) — prioridade atual do dono:
  terminar o MVP da API de mensageria do Portal (estilo WhatsApp, documentada, adaptável a qualquer
  produto) antes de retomar `PLANO_ATUALIZADO_LOGIN_E_FLUXO_PACIENTE_PRECHECK.md`. Duas peças deste
  repo:
  - `z_prompts/PROMPT_BRAIN_MESSAGE_PORTAL_API_REFERENCE_DOC.md` (Sonnet 5, médio) — cria
    `docs/PORTAL_MESSAGING_API.md`, o documento de referência único do canal Brain-Message (hoje
    espalhado em ~15 `CHECKPOINT_*` sem nenhuma seção em `CONTRACTS.md`). Só documentação, sem
    dependência. **EXECUTADO em 2026-09-18** — `docs/PORTAL_MESSAGING_API.md` criado (10 seções +
    changelog, cada afirmação verificada contra o código nesta data), com o achado de que a
    seção "enviar clínica→paciente" do prompt original estava desatualizada (o bug de despacho
    do console já tinha sido corrigido na Onda 0, ver acima) — o documento reflete o código
    real, não a suposição do prompt. 4 ponteiros de 1 linha adicionados neste arquivo e nos
    `CLAUDE.md` de `secretarIA`, `PreCheck` e `Brain-Message-Frontend`. Nenhum código de produto
    mudou.
  - `z_prompts/PROMPT_BRAIN_MESSAGE_ANEXOS_SECRETARIA_1_BRAIN_API.md` (Opus 5, alto) — anexo de
    arquivo no canal Brain-Message, revertendo a decisão da `PARIDADE_4` acima: sai para o produto
    `secretaria` primeiro (a cadeia PreCheck nunca rodou), com PreCheck explicitamente adiado.
    Multipart de ponta a ponta, validação de tipo real + tamanho, streaming autenticado de volta ao
    navegador (nunca URL assinada direta, mesmo motivo de CSP que a `PARIDADE_4` já havia registrado).
    Depende da parte 2 (secretarIA) para ir a produção; pode ser codado com mock antes.
    **EXECUTADO em 2026-09-18 — BUILT, não commitado, não deployado.** Mesma rota em dois encodings
    (JSON intacto; multipart com `file`), corpo lido dentro do handler (auth antes do 1º byte),
    tipo pelos magic bytes, 20 MiB, upload por qualquer paciente — inclusive e-mail não
    verificado, decisão do dono —, rota
    `GET /patient-access/threads/{product}/media/{message_id}` em stream, referência na listagem
    sem chave de armazenamento. Nova dependência `python-multipart` (não existia). Contrato que a
    parte 2 consome, decisões e provas em `docs/CHECKPOINT_brain_message_anexos.md`; resumo em
    `docs/PORTAL_MESSAGING_API.md` §7. Ordem de deploy: secretarIA parte 2 → este → frontend.
  - `z_prompts/PROMPT_BRAIN_MESSAGE_STATUS_ENTREGA_2_BRAIN_API.md` (Opus 5, médio-alto; adicionada
    2026-09-18, achado do dono) — Onda 3: estado de entrega/leitura (✓/✓✓) nunca funcionou de
    verdade (evento do WhatsApp descartado, frontend fixo em "enviado" — causa raiz completa na
    parte 1 deste item, repo secretarIA). Este repo ganha `GET .../messages` repassando o status
    real e `POST /patient-access/threads/{product}/messages/read`, sempre com `tenant_id`/
    `external_id` tirados da sessão, nunca do corpo. Sequencie DEPOIS da peça de anexos acima
    (mesmos arquivos). **NÃO EXECUTADO.**
  - `z_prompts/PROMPT_BRAIN_MESSAGE_ANEXOS_BRAIN_API_UPLOAD_CRASH.md` (Opus 5, alto; gerado
    2026-09-18 via `/prompt-generator` a partir de um teste ao vivo em produção) — **bug de
    produção achado ao testar a peça de anexos acima já deployada**: qualquer mensagem de texto
    funciona (200), multipart sem arquivo dá 422 correto, mas multipart **com** um arquivo real
    derruba a conexão com o brain-api (502 "Service is not reachable" — a página do PRÓPRIO
    EasyPanel, não um JSON do brain-api, o que aponta pro processo crashando, não pro secretarIA
    fora do ar). Suspeito principal: `message_switchboard.py::send_attachment` construindo
    `httpx.Request` multipart numa thread, forçando `fileno()`/rollover do `SpooledTemporaryFile`
    do Starlette — mas não confirmado (log do EasyPanel bloqueado por `401 Unauthorized` do MCP
    nesta sessão). Divergência achada: o teste que exercita esse caminho passa localmente no
    Windows (`uv run pytest`) mas nunca rodou no container Linux real da imagem de produção.
    **EXECUTADO 2026-09-19 — premissa do prompt FALSA, não havia crash** (logs dos dois containers;
    o projeto EasyPanel é `secretaria`, não `secretara`): a secretarIA recusou o arquivo com `409
    attachment_consent_required` (visitante sem aceite LGPD, por desenho), este repo achatou em `502
    product_error`, e o gateway do EasyPanel troca todo 502 do app pela página HTML dele. Correção:
    `core/attachments.py::PRODUCT_REFUSALS` (409 consentimento, 429 cota) repassadas como 4xx por
    `message_switchboard._call(refusals=...)`. Causa, provas e o achado "todo 502 vira HTML" em
    `docs/CHECKPOINT_brain_message_anexos.md` §10.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
