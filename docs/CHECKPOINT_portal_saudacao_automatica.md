# CHECKPOINT — a automação fala primeiro no Portal (perna brain-api da TASK-003)

> Feature checkpoint (`AI_WORKFLOW.md` — "feature grande/multi-camada"). **Perna brain-api
> apenas**, das três da TASK-003 (`BRAIN/tasks/TASK-003/TASK.md`). As outras duas —
> `Brain-Message-Frontend` (tela de links, fim do `OtpLogin`, 3 links) e `secretarIA`
> (`/internal/brain-message/open`, card de 3 botões, handoff pós-agendamento) — têm donos
> próprios e não foram tocadas daqui.
>
> **O contrato não é duplicado aqui.** Ele vive em `PORTAL_MESSAGING_API.md` §8.6 (a rota de
> abertura e os dois gatilhos), §8.7 (`email_masked`) e `CONTRACTS.md` §12.3 + §12.3.2 (o
> handoff com `external_id` e por que ele para em 501). Este arquivo carrega só o que aqueles
> não carregam: estado, decisões tomadas sem perguntar, ordem de deploy e o que não foi provado.

**Rodada:** 2026-09-19 · **Branch:** `task/TASK-003-brain-api` (base `7796949`) ·
**Estado: BUILT, commitado NA BRANCH, não mergeado em `main`, NÃO deployado** ·
**Sem migração** (nenhuma coluna nova; nada para aplicar em produção).

## 1. O que entrou

| Peça | Arquivo |
|---|---|
| Saudação automática (cliente) | `src/brain_api/services/message_switchboard.py` — `open_conversation` (fire-and-forget, nunca levanta) + `default_product` |
| Saudação automática (gatilhos) | `src/brain_api/api/patient_access.py` — `_greet_if_secretaria`, chamado de `open_pending` (só em visita CRIADA) e de `add_clinic_by_invite` |
| Máscara de e-mail | `src/brain_api/core/email_mask.py` (**novo**, função pura) + `schemas/internal.py::PendingOtpRequestOut.email_masked` + `api/internal.py::request_pending_otp_internal` |
| Handoff do Portal | `schemas/internal.py::PrecheckHandoffIn` (`external_id` XOR `phone_number`) + `api/internal.py::precheck_handoff` (ramo `501`) |
| Testes | **+79**: `tests/test_email_mask.py` (**novo**, 33), `tests/test_portal_auto_greeting.py` (**novo**, 26), `tests/test_precheck_handoff.py` (35 → 51), `tests/test_patient_pending_session.py` (28 → 32, e 1 asserção de igualdade exata atualizada). Suíte: **717 → 796 passando, 0 falhando** nas duas pontas |
| Contrato (doc) | `CONTRACTS.md` §12 (linha da tabela), §12.3 (dois handles), **§12.3.2** (nova); `docs/PORTAL_MESSAGING_API.md` **§8.6** e **§8.7** (novas) |

## 2. Decisões tomadas sem perguntar

1. **`BackgroundTasks`, não `await` com timeout curto.** O contrato pede que uma falha da
   saudação não derrube nem atrase a resposta. Um timeout curto *limita* o dano; rodar depois
   do corpo da resposta o *elimina*: quando a tarefa roda, a resposta do paciente já foi
   escrita, então não há nada que possa falhar nem atrasar. Timeout continua sendo
   `SECRETARIA_TIMEOUT_SECONDS` — nenhum knob novo, porque a garantia não vem dele.
2. **`open_conversation` mora em `message_switchboard.py` e é a única função do módulo que não
   levanta.** O módulo é "fail closed, loudly" porque todo hop dele tem um paciente esperando;
   este não tem. Reusar o módulo (mesma chave, mesmo header, mesmo `_upstream`) em vez de
   inventar um cliente novo era a instrução, e a exceção está documentada na própria docstring.
3. **"Produto resolvido" = `payload.product or default_product(ent)`**, e `default_product` é o
   primeiro de `available_products` — a MESMA lista que decide as abas do Portal. Não foi
   hardcodado `"secretaria"`: uma clínica só-PreCheck cai no PreCheck, e a regra daqui não pode
   divergir do que o paciente vê.
4. **O gatilho de `POST /patient-access/clinics` dispara em TODA adição bem-sucedida**, sem
   tentar descobrir se a clínica era nova para a conta. A pergunta honesta não é "a linha é
   nova" (a identidade pode ser anterior à conta) e sim "existe conversa com mensagem", que só
   a secretarIA sabe. Ela responde `exists` sem enviar nada — delegar a idempotência é o que
   torna dois gatilhos seguros sem estado aqui. (Decisão alinhada com a correção do contrato
   §2 de 2026-09-19.)
5. **`external_id` é `UUID`, não o `str | None` literal do contrato.** O handle É um
   `MessagePatient.id`, e os dois schemas irmãos (`PendingEmailClaimIn`, `PendingIdentityIn`)
   já o tipam assim. Consequência: um valor que nunca poderia ser um handle vira `422` aqui em
   vez de um 404 três hops adiante. **Desvio registrado** — se a secretarIA mandar algo que não
   é UUID, o 422 é deste schema, não um bug dela.
6. **`501`, não `503` nem `422`, no ramo `external_id`** — ver `CONTRACTS.md` §12.3.2.
7. **`email_masked` só na rota INTERNA**, não na pública `POST /patient-access/pending/request-otp`.
   Quem desenha o card é a secretarIA; o navegador não tem uso para o campo, e cada campo a mais
   numa resposta pública é superfície que alguém vai acabar consumindo.
8. **`email_masked` é obrigatório (`str`), não `str | None`.** Num `200` sempre existe endereço:
   uma visita sem endereço é o `409 pending_email_missing`, nunca um `200` com `null`.
9. **`open_conversation` tem um `except Exception` além do `except httpx.HTTPError`.** É o único
   catch cego do módulo e é carga, não preguiça: `httpx.InvalidURL` **não** é um `HTTPError`
   (herda direto de `Exception` — conferido), então um erro de digitação do operador em
   `SECRETARIA_BASE_URL` escaparia de uma background task e apareceria como "Exception in ASGI
   application" *depois* de uma resposta perfeitamente boa — o modo de falha mais confuso
   possível. `Exception` e não `BaseException`, para um `CancelledError` de shutdown continuar
   propagando. Coberto por `test_a_malformed_base_url_never_fails_the_patients_route`.
10. **`ruff format` rodado em 3 arquivos que JÁ eram "would reformat" no HEAD**
   (`api/internal.py`, `api/patient_access.py`, `services/message_switchboard.py`). Isso
   contraria a prática registrada em `CHECKPOINT_precheck_handoff_context.md` §4 de não
   reformatar código pré-existente — feito mesmo assim porque o custo real foram **3 hunks de
   uma linha cada** (uma string de `description`, um `return ThreadListOut(...)`, um
   `raise HTTPException(...)`, todos só re-unidos numa linha) e o ganho é que o `ruff format
   --check` volta a ser um sinal útil sobre o código NOVO desses arquivos, que de outro modo
   ficaria mascarado pela falha pré-existente. Reverter os 3 hunks leva segundos se o revisor
   preferir.

## 3. Ordem de deploy — LIVRE nos dois sentidos

Diferente de quase toda mudança de contrato desta malha, esta perna **não** tem ordem obrigatória:

- **brain-api antes da secretarIA**: `POST /internal/brain-message/open` ainda não existe lá →
  `404` → `open_conversation` registra `brain_message_open_refused` e devolve `OPEN_FAILED`.
  Nenhuma saudação acontece, que é exatamente o comportamento de hoje. Nada quebra.
- **secretarIA antes do brain-api**: a rota nova simplesmente não é chamada por ninguém.
- `email_masked` é campo **adicionado numa resposta** (não num request `extra="forbid"`), então
  não tem a armadilha da `frozen-contract-migration`. **Premissa não verificada daqui**: que a
  secretarIA leia essa resposta com um parser tolerante a campo extra — quem fecha isso é a
  perna C (não foi possível conferir o repo dela nesta worktree).
- `PrecheckHandoffIn` **ampliou** o conjunto de campos conhecidos e manteve `extra="forbid"`; o
  corpo legado de dois campos continua idêntico. Mandar `external_id` de uma secretarIA nova
  para um brain-api velho, porém, é `422` — **esta perna tem que estar viva antes** de a
  secretarIA começar a mandar `external_id` (mesma regra da §12.3.1).

## 4. Revisão

`ecc:fastapi-reviewer` e `ecc:python-reviewer` rodaram sobre o diff. Achados e tratamento
completos em `BRAIN/tasks/TASK-003/results/brain-api.md` §3b. Resumo do que MUDOU por causa
deles:

- `core/email_mask.py` ganhou `_clusters()`: o NFC sozinho não impede marca combinante solta
  (`a` + agudo + circunflexo compõe `á` e deixa o circunflexo pendurado), então os caracteres
  das pontas passaram a ser base + suas marcas, e marca sem base devolve `***`.
- Três testes de fire-and-forget afirmavam só `200` — que já era verdade antes da feature.
  Agora afirmam também que a chamada foi tentada. **Mutation test**: apagar
  `background_tasks.add_task(...)` produz 14 falhas (antes, 9 desses passariam).
- Os testes de XOR passaram a afirmar a mensagem do validador / o `loc` do erro, porque
  `extra="forbid"` devolveria 422 para os mesmos corpos pelo motivo errado.
- Ficou registrado, **sem correção**, um MEDIUM pré-existente: `core/ratelimit.py::client_ip`
  confia em `X-Forwarded-For`, e agora isso tem fan-out para a malha. Causa raiz é repo-wide e
  fora do escopo desta tarefa; a amplificação é 1:1 com um abuso pior que já existia (criar
  linhas no banco). Argumento completo no `results/brain-api.md`.

## 5. Uma regressão causada e corrigida

Três testes de `tests/test_patient_attachments.py` quebraram: eles usam `POST /pending` como
**setup** e depois afirmam "exatamente N hops na malha" — a saudação virou um hop a mais.
Contador medindo duas features, não bug de produção. O helper `_mesh` passou a **responder** ao
hop da saudação sem registrá-lo (responder, e não deixar passar, evita que cada teste espere uma
conexão real a um host inexistente). `tests/test_patient_read_receipts.py` importa o mesmo helper
e foi coberto junto. **Qualquer teste futuro que conte hops da malha depois de abrir uma visita
precisa da mesma ressalva.**

## 6. Pendências

- [ ] Merge da branch em `main` (Integrator) e deploy — **NÃO AUTORIZADO** até o dono pedir.
- [ ] **A conversa do PreCheck continua nascendo vazia** — nenhum gatilho para `product="precheck"`.
      É a limitação conhecida da TASK-003 §2, e a mesma lacuna que a §12.3.2 descreve pelo outro
      lado: falta uma rota **no repo do PreCheck** que abra/garanta sessão por
      `(brain_tenant_id, session_ref)` sem mensagem.
- [ ] Prova ponta a ponta da saudação — depende da rota da secretarIA existir e das duas pernas
      estarem deployadas. Aqui só está provado o que brain-api ENVIA.
