# CHECKPOINT — Hub token: quem vê a agenda inteira (`agenda_scope`) (TASK-044)

Plano: `docs/superpowers/plans/2026-10-09-hub-token-papel-profissional.md`.
Spec: `secretarIA/docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md` §5.A,
na workspace BRAIN. Esta execução cobre somente o produtor brain-api; R7 e R5 seguem separados.

## Estado

- Branch `task/TASK-044-hub-token-papel`, worktree `C:\TECH\BRAIN-worktrees\TASK-044\brain-api`.
- Base `b1a289ad62faaa03d69a0c7db562c1017d1b9f00` (`origin/main` na abertura).
- Commits de implementação: `cf0833f` (regra/resolver), `7f3331d` (introspecção/testes).
  Este documento acompanha o commit `docs(internal): agenda_scope contract and checkpoint (TASK-044)`.
- **Local, não mesclado, não pushado, não deployado. Sem migração no brain-api.**

## O que mudou

- `services/hub_scope.py::agenda_scope_for`: regra pura, sem derivar permissões do token.
- `resolve_agenda_scope`: lê o usuário nomeado por `act`, verifica a clínica e aplica o cadastro atual.
- `POST /internal/secretaria/hub-token/verify` responde também `agenda_scope: "clinic" | "own"`
  (`schemas/internal.py::HubTokenVerifyOut`). Token, emissão, `active`, `tenant_id` e
  `professional_id` mantêm o comportamento anterior.
- Campo aditivo, padrão `"own"`. O consumidor atual lê `body.get` e ignora a chave nova;
  o consumidor R7 fará a aplicação da regra. Mudanças de papel valem na próxima introspecção,
  sujeitas ao cache positivo da secretarIA (padrão 60 s).

## Regra

`secretary`, `manager`, `doctor` com `is_owner`/`is_manager`, legado `tenant_owner` → `"clinic"`.
Qualquer outro (médico comum, legado `tenant_staff`, `admin`, papel desconhecido,
ator desconhecido/malformado/removido ou de outra clínica, token recusado) → `"own"`.
Para `"own"`, R7 restringe às consultas de `professional_id`; sem profissional, nenhuma consulta.

## Validação

- TDD da regra/resolver: RED por módulo ausente → **19 passed**.
- Introspecção: RED por `agenda_scope` ausente → GREEN. Inclui testes adicionais para
  dono com assinatura inativa/cancelada, produto desabilitado e atores JSON malformados.
- `uv run pytest tests/test_hub_agenda_scope.py tests/test_auth_hardening.py -q`:
  **66 passed**, 77.11 s.
- Baseline em `b1a289a`, antes da implementação: `uv run pytest -q` →
  **1355 passed, 3 skipped, 39 warnings**, 2063.96 s.
- Suíte completa com a mudança, em `7f3331d`: `uv run pytest -q` →
  **1391 passed, 3 skipped, 39 warnings**, 2167.35 s. Nenhuma falha; 36 casos adicionados.
- `uv run ruff check .`: passou no baseline e após a implementação.
- `uv run ruff format --check .`: baseline com **31 arquivos anteriores** fora do escopo;
  os mesmos 31 continuam pendentes. Nenhum foi reformatado. Os quatro arquivos Python
  desta mudança passaram na checagem de formatação.
- Graphify atualizado por AST e validado: 4409 nós, 10137 arestas, zero duplicatas exatas
  ou pontas ausentes; um self-loop incidental, permitido pela política. Os snapshots gerados
  permanecem locais, fora dos commits de implementação/documentação deste plano.

## Decisão de execução

O trecho sugerido na Task 2 resolvia o papel mesmo para uma assinatura recusada, podendo
devolver `"clinic"` a um dono com `active:false`. Foram seguidos os Global Constraints e
Review Focus: resolver somente quando `active` é verdadeiro; qualquer recusa recebe `"own"`.
Se essa escolha estivesse errada, seu efeito seria apenas o alcance informado para sessões
já recusadas; a recusa e os ids preservam o comportamento anterior.

## Deploy (quando o dono autorizar)

Migração prevista da secretarIA (`d8e3a5c1f7b2`) → **este brain-api** →
`secretaria_api` + `secretaria-worker` juntos → front (R5).
Este deploy sozinho não muda nada visível, pois a secretarIA atual ignora o campo.
Sintoma de ordem trocada: gestor que também é médico só vê as próprias consultas e não vê
o seletor "Todos / Só os meus". Nenhuma etapa de produção foi executada.

## Pendências (fora deste plano)

- `GET /doctor/appointments` e `GET /doctor/patients` continuam listando a clínica inteira
  para os papéis aceitos por `require_doctor`; esta regra vale para o hub da secretarIA.
  A eventual aplicação nessas listas depende de decisão separada do dono.
- Execução do consumidor R7 e do front R5; revisão e publicação da cadeia nas etapas próprias.
