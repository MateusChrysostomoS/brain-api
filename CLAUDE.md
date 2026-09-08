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
  `..._OTP_SWITCHBOARD.md` continua PENDENTE.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
