# Checkpoint — digitando backend (TASK-033)

2026-10-04. Plano: `secretarIA/docs/superpowers/plans/2026-10-01-digitando-backend.md`.
Task 6 implementada em worktree. Sem migração. Em 2026-10-04 o dono autorizou testes com agent-browser, merge e push após testes verdes; deploy não autorizado.
Contrato canônico: `PORTAL_MESSAGING_API.md`, seção “Typing — o contrato de digitação, qualquer produto”.

- `POST /patient-access/threads/{product}/typing`: corpo vazio fechado; escopo da sessão de conta
  autenticada por clínica ou visita pendente; autorização de produto/canal antes de rede;
  conexão de banco liberada antes do salto.
- `TYPING_PRODUCTS` registra secretarIA em `/internal/brain-message/typing`; PreCheck devolve
  `payload.applied: false` sem rede. Campos `typing`, `typing_by`, `accepts_typing` passam intactos
  pela listagem existente.
- Na secretarIA: Redis `brain_message:typing:<conversation_id>:<by>`; TTL automação 90 s,
  equipe/paciente 6 s; paciente só registrado com humano (`HUMAN_ACTIVE`). Paciente nunca vê
  a si mesmo e console nunca vê equipe. Falhas Redis são abertas; consultar checkpoint irmão
  para evidências da implementação dessa perna.

## Validação local

Baseline focado: 80 testes passando. RED: 11 falhas esperadas 404 e 1 poll já passando;
GREEN: 12 testes novos passando. Focado final: 92 passando, incluindo conta/visita, corpo
fechado, autorização de produto/canal, PreCheck sem rede e fechamento de sessão antes de upstream.
Runner temporário em `.superpowers/sdd/2026-10-01-digitando-backend/hermetic_pytest.py` bloqueia
transporte HTTP externo e preserva ASGI/MockTransport; spies dos testes interceptam o mesh.
`ruff check .` passa. `ruff format --check .` tem 32 arquivos pré-existentes fora do padrão;
as três fontes alteradas já falham no HEAD; novo teste está formatado. Não houve reformatação ampla.
Suíte completa hermética pré-merge: 1252 passed/3 skipped/39 warnings de depreciação existentes em 781.30s, exit 0. Integrador reexecutou12 testes novos e lint, ambos verdes. Revisão independente sem achados nesta perna; os dois achados de ciclo de vida na secretarIA foram corrigidos com RED→GREEN. Ver `C:/TECH/BRAIN/tasks/TASK-033/FINAL_RESULT.md`.

## Navegador e deploy

agent-browser 0.34.0 + Chrome executaram o build real do Brain-Message-Frontend contra as rotas e autenticação reais dos dois backends, por HTTP local. Dados sintéticos em SQLite, Redis emulado com Lua e fila/LLM controlados; nenhuma chamada externa. Passaram abertura de visita, login da clínica, digitação nos dois sentidos, automação/concorrência/cancelamento, expiração humana, envio/cleanup, 404 cross-tenant, 401 anônimo, 422 para tentativa de escolher identidade pelo corpo e disponibilidade do chat sem Redis. Evidências e limite da prova de expiração da automação em BRAIN tasks/TASK-033/BROWSER_RESULT.md.

Deploy não autorizado. Ordem quando solicitado: `secretaria_api` + `secretaria-worker` → brain-api. Redis/PostgreSQL reais, latência/ACL e interrupção de processo do worker implantado permanecem sem prova nesta execução local.