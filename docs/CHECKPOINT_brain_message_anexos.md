# CHECKPOINT — Anexos no Portal (Brain-Message), parte 1: borda do brain-api

**Estado (2026-09-18): BUILT — não commitado, não deployado. Deploy: NOT AUTHORIZED.**
Prompt: `z_prompts/PROMPT_BRAIN_MESSAGE_ANEXOS_SECRETARIA_1_BRAIN_API.md` (parte 1 de 3 da Onda 2 de
`z_prompts/PLANO_PORTAL_API_MVP.md`). Base `main@3ee89b7`, checkout normal (sem worktree — ver §9).
Este arquivo é **o contrato que a parte 2 (secretarIA) consome**; o resumo público está em
`docs/PORTAL_MESSAGING_API.md` §7. Os números moram num lugar só: `src/brain_api/core/attachments.py`.

## 1. Causa reconfirmada no código

- `POST /patient-access/threads/{product}/messages` só aceitava JSON (`PatientMessageIn`,
  `extra="forbid"`: `text`, `patient_name`, `interactive_reply_id`). Qualquer anexo dava 422.
- Mais fundo do que o prompt dizia: **o brain-api nem tinha `python-multipart` instalado**, então
  nenhuma rota dele conseguia ler `multipart/form-data`. Agora está adicionado
  (`python-multipart==0.0.32` em `pyproject.toml` + `uv.lock`; o Dockerfile usa
  `uv sync --frozen`, então a imagem já pega).
- `GET .../messages` é passthrough opaco (`RelayOut.payload`): não havia onde a referência do anexo
  aparecer, nem rota para buscar o arquivo de volta.

## 2. O que entrou (âncoras estáveis)

| Arquivo | O quê |
|---|---|
| `core/attachments.py` (novo) | `MAX_ATTACHMENT_BYTES`, `MULTIPART_OVERHEAD_BYTES`, `ALLOWED_KINDS`, `sniff_kind`, `safe_filename`, `extension_conflicts`, `check_attachment`, `REFUSALS` + `AttachmentRefused`, `MEDIA_ID_PATTERN`/`is_media_id` |
| `api/patient_access.py` | `send_thread_message` (JSON **ou** multipart), `get_thread_identity`, `_relay_attachment`/`_checked_relay`, `_CappedReceive`, `_read_json_message`, `_attachment_limiter`, `get_thread_media`, `_media_headers` |
| `services/message_switchboard.py` | `ATTACHMENT_PRODUCTS`, `send_attachment`, `_project_attachments`/`_attachment_ref`, `open_media`/`MediaStream`, `_capped`; `_call` ganhou `data`/`files`/`timeout` opcionais |
| `schemas/patient_access.py` | `PatientAttachmentForm` (campos de texto do multipart) |
| `config.py` | `PATIENT_ATTACHMENTS_ENABLED`, `PATIENT_ATTACHMENT_RATE_LIMIT_PER_MIN`, `ATTACHMENT_UPSTREAM_TIMEOUT_SECONDS` |
| `tests/test_patient_attachments.py` (novo) | 23 testes (12 funções + parametrizações; ver §8) |

## 3. Contrato navegador → brain-api

### 3.1 Enviar com anexo

`POST /patient-access/threads/{product}/messages`, `Content-Type: multipart/form-data`,
`Authorization: Bearer <token>` — de clínica (`patient_message`) **ou** pendente (`patient_pending`).

| Parte | Regra |
|---|---|
| `file` | exatamente uma; JPEG/PNG/WEBP/GIF/PDF **pelo conteúdo** (magic bytes); de 1 a 20 971 520 bytes |
| `text` | legenda opcional, ≤ 4000 caracteres; vazio = ausente (arquivo sem legenda é mensagem completa) |
| `patient_name` (≤ 200), `interactive_reply_id` (1–256) | iguais ao corpo JSON |
| qualquer outro campo | 422 `extra_forbidden`, `loc: ["body", "<campo>"]` — nenhum campo nomeia clínica ou paciente |

Resposta de sucesso: `200` + `RelayOut` (`payload` = ack da secretarIA, hoje `{"status": "queued"}`).

- Tipo repassado = o **farejado** nos bytes; `Content-Type` do navegador e extensão nunca contam.
  Extensão errada da mesma família (PNG chamado `.jpg`) é corrigida no nome; família cruzada (PDF
  chamado `.jpg`) ou extensão fora da lista (`.exe`) é recusada. Nome saneado: só o último segmento
  do caminho, sem caracteres de controle/reservados/bidi, sem ponto inicial, ≤ 120 caracteres.
- Qualquer paciente, com e-mail verificado ou não (decisão do dono, §6 decisão 3); o visitante
  pendente gasta também um orçamento compartilhado da clínica.
- Só produtos em `ATTACHMENT_PRODUCTS` (hoje `secretaria`); PreCheck → 422, como antes.

### 3.2 Enviar sem anexo

Idêntico ao anterior (`application/json`, `PatientMessageIn`). A rota agora lê o próprio corpo (§6,
decisão 1), replicando a regra de *strict content type* do FastAPI 0.137: mesmos 422
(`missing`, `json_invalid`, `model_attributes_type`, erros do modelo sob `body`).

### 3.3 Listagem (`GET .../messages`)

Cada mensagem com `attachment` não nulo sai reescrita para exatamente
`{"content_type", "size_bytes", "filename", "media_path"}` (whitelist; chave de objeto R2 ou URL
assinada é descartada; referência malformada vira `null` + log `switchboard_attachment_ref_invalid`).
`media_path` = `/patient-access/threads/{product}/media/{message_id}`, relativo à raiz do brain-api
(no Portal: prefixar `/api/brain`). Mensagens sem a chave passam intactas.

### 3.4 Baixar: `GET /patient-access/threads/{product}/media/{message_id}`

- Bearer igual ao da thread (clínica **ou** pendente — o visitante lê o que a clínica mandou a ele).
- `200`: bytes em stream; `Content-Type` ∈ os 5 tipos; `X-Content-Type-Options: nosniff`;
  `Content-Security-Policy: default-src 'none'; sandbox`; `Cache-Control: private, no-store`;
  `Cross-Origin-Resource-Policy: same-origin`; `Content-Disposition` `inline` (imagem) /
  `attachment` (PDF) com nome genérico `anexo.<ext>` (o nome real, possível PII, fica no JSON);
  `Content-Length` quando o upstream informa.
- `404 attachment_not_found` — **mesmo corpo** para "não existe", "é de outra conversa" e "produto
  sem anexos". `422` para id fora de `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`. `502`/`503` como no resto.
- **Nota para a parte 3:** por exigir o header `Authorization`, `<img src>` não busca sozinho — o
  front faz `fetch` com o bearer e renderiza os bytes. A CSP atual do Portal
  (`img-src 'self' data:`) então exige `data:` (via `FileReader`) **ou** incluir `blob:` em
  `img-src`. Decisão da parte 3. Atenção (revisão de segurança): uma URL `blob:` perde os headers
  acima e herda a origem do Portal — mostrar imagem só em `<img>`, PDF só como download, e nunca
  abrir um `blob:` como página/aba.

## 4. Contrato brain-api → secretarIA (o que a parte 2 PRECISA implementar)

Todas as chamadas com `X-Internal-Api-Key` (mesma chave de hoje). `tenant_id` e `external_id`
sempre vêm da sessão do paciente, nunca do navegador.

### 4.1 `POST /internal/brain-message/inbound` — variante multipart

- `Content-Type: multipart/form-data; boundary=...` no **mesmo path** da variante JSON (que continua
  idêntica para mensagens sem arquivo). secretarIA precisa aceitar os dois encodings.
- Campos: `tenant_id` (UUID), `external_id` (`patient_ref`), `text` (omitido quando não há legenda),
  `patient_name`, `interactive_reply_id` (omitidos quando ausentes); parte `file` com `filename`
  saneado (extensão já coerente com o tipo) e `Content-Type` = tipo farejado (um dos 5).
- secretarIA **revalida** com os mesmos números (`MAX_ATTACHMENT_BYTES` = 20 971 520; os 5 tipos;
  magic bytes) e **deve aceitar tudo que o brain-api aceitou**. Se recusar mesmo assim, o brain-api
  responde `502 product_error` ao paciente e loga `switchboard_upstream_error` com o status — é
  deriva de contrato (bug), não erro do paciente.
- Resposta esperada: igual à do JSON (`202 {"status": "queued"}`). `503` é repassado como `503`
  (ex.: R2 fora do ar — retentável). Timeout do brain-api: `ATTACHMENT_UPSTREAM_TIMEOUT_SECONDS`
  (60 s por operação de rede, não pelo arquivo inteiro).
- Recomendação: ler o corpo dentro do handler (como o brain-api faz) em vez de declarar
  `UploadFile` — o FastAPI parseia corpo declarado antes das dependências, inclusive a da chave.
- **Obrigatório na parte 2: cota PERSISTIDA de bytes por paciente e por clínica** (ex.: por dia),
  aplicada onde o arquivo é gravado. O limite daqui é só contagem (10 uploads/min por paciente e,
  para não verificados, 20/min por clínica; em memória, por processo, fail-open) — sem cota, uma
  identidade grava até ~288 GiB/dia (revisão de segurança, achado MEDIUM). Com upload aberto a
  visitante não verificado, cuja identidade custa uma chamada, **a cota por CLÍNICA é a que de fato
  limita**. Recusa por cota = 4xx permanente com `code` próprio no mesmo formato do §5 (o brain-api
  hoje a repassaria como `502 product_error` — ao definir o `code`, avisar para o brain-api passar
  a repassá-lo).
- **Decidir na parte 2: arquivo antes do aceite LGPD.** Com upload aberto a quem não verificou o
  e-mail, um arquivo (possível dado de saúde) pode chegar antes do aceite LGPD daquela conversa. A
  secretarIA é dona desse gate; recomendação: recusar com `code` permanente até o aceite, sem gravar
  nada (mesma ressalva de repasse acima).

### 4.2 `GET /internal/brain-message/conversations/{external_id}/messages`

Acrescentar por mensagem `attachment: {"content_type", "size_bytes", "filename"} | null` (o `id`
já existe). `size_bytes` inteiro em (0, 20 971 520]; `content_type` um dos 5. **Nenhum campo** da
listagem — dentro ou fora de `attachment` — carrega chave de objeto R2 ou URL assinada: o
brain-api só reescreve a chave `attachment`; qualquer outro campo passa direto ao navegador.

### 4.3 `GET /internal/brain-message/media/{message_id}?tenant_id=...&external_id=...` (nova)

- `200`: bytes crus do arquivo, `Content-Type` = o tipo gravado; `Content-Length` recomendado; sem
  redirect, sem URL.
- `404` quando a mensagem não existe, não pertence à conversa `(tenant_id, external_id)` ou não tem
  anexo — **uma resposta só**. `503` para falha transitória do armazenamento.
- **A checagem de dono vive AQUI, não no brain-api**: buscar numa consulta só por
  `(message id, tenant_id, external_id)` — nunca pelo id e depois conferir. O brain-api garante só
  que os dois valores vêm da sessão; quem impede o paciente B de ler o arquivo do A é esta rota.
- `message_id` = `messages.id` (UUID) da secretarIA.

## 5. Taxonomia de recusas (4xx — permanentes: reenviar o mesmo arquivo nunca passa)

Corpo: `{"detail": {"code": "...", "message": "..."}}` (+ `max_bytes` no 413). `message` em
português, para exibir como está. Tabela-fonte: `core/attachments.py::REFUSALS`.

| `code` | HTTP | Quando |
|---|---|---|
| `attachment_too_large` | 413 | arquivo > 20 971 520 bytes, ou corpo acima do teto (declarado ou em stream) |
| `attachment_type_unsupported` | 415 | conteúdo não é nenhum dos 5 tipos (HTML, SVG, BMP…) |
| `attachment_type_mismatch` | 422 | nome promete outra família, ou extensão fora da lista |
| `attachment_empty` | 422 | 0 bytes |
| `attachment_malformed` | 422 | sem `file`, dois arquivos, campo repetido, multipart quebrado |
| `attachment_unsupported_for_product` | 422 | produto sem anexos (PreCheck) ou kill switch desligado |
| `attachment_not_found` | 404 | rota de mídia, qualquer motivo |

Também: `429` (orçamento por paciente; para visitante não verificado, também o compartilhado da
clínica), `401`, `403 product_unavailable`, `502`, `503` como no resto de `/patient-access`.

## 6. Decisões tomadas sem consulta (e como reverter)

1. **Mesma rota, dois encodings, corpo lido dentro do handler.** Com `UploadFile` declarado, o
   FastAPI parseia (e grava em spool) o upload **antes** de rodar a autenticação — qualquer um sem
   sessão faria o serviço gravar uploads sem limite. Aqui sessão, entitlement, produto, verificação e
   orçamento vêm antes do primeiro byte; o teto vale no `Content-Length` e, para corpo *chunked*,
   durante o stream (`_CappedReceive`).
2. **Recusa = 4xx com `code`, não 200.** A skill `brain-mesh-permanent-vs-transient-refusal` admite
   4xx quando o erro é da própria requisição; um 200 faria o front marcar como "enviado" um arquivo
   que nunca saiu. Nenhuma recusa é 5xx.
3. **Qualquer paciente envia arquivo, inclusive com e-mail não verificado (token pendente) —
   decisão do dono, 2026-09-18**, revertendo a versão anterior desta mesma sessão (que exigia token
   de clínica e respondia 403 `attachment_requires_verified_patient`; o código foi removido). Como
   `POST /pending` cria identidade nova — e com ela orçamento por paciente novo — a cada chamada,
   os uploads NÃO verificados de uma clínica dividem um orçamento próprio
   (`PATIENT_PENDING_ATTACHMENT_RATE_LIMIT_PER_MIN`, 20/min por clínica, `0` desliga); o paciente
   verificado nunca o gasta. O arquivo enviado antes do código fica na conversa do handle da visita
   (o mesmo `patient_ref` depois de verificar); se a visita for substituída por uma identidade que
   a clínica já tinha (`superseded_by`), ele fica na conversa antiga, igual ao texto.
4. **Limites como constantes, não env var** — env var faria brain-api e secretarIA discordarem.
5. **Tipo por magic bytes; PDF só no offset 0** (recusa poliglotas); SVG fora (imagem com script).
6. **Rate limit:** novo `_attachment_limiter` no mesmo `SlidingWindowLimiter`, por paciente,
   10/min. Sem limitador por bytes: o teto implícito é 10 × 20 MiB/min por paciente (por processo).
   Mais `_pending_attachment_limiter`, por clínica, só para os não verificados (decisão 3).
7. **Kill switch** `PATIENT_ATTACHMENTS_ENABLED` (default `true`): desligado, todo produto responde o
   mesmo 422 do PreCheck; leitura de arquivos já gravados continua.
8. **Timeout** de 60 s nos dois saltos com arquivo (`ATTACHMENT_UPSTREAM_TIMEOUT_SECONDS`).
9. **Recusa da secretarIA depois do aceite do brain-api → 502** (deriva de contrato, não do paciente).
10. **Resposta de mídia endurecida** (§3.4): tipo reconferido (um `text/html` do upstream vira 502,
    nunca stream), comprimento declarado acima do teto → 502, corpo cortado no teto.
11. **Correção honesta do prompt:** "sem nunca escrever em disco" não é literal. O Starlette mantém até
    1 MiB por parte em memória e depois um arquivo temporário anônimo — e a medição de tamanho do
    httpx (`fileno()`) faz até um arquivo pequeno ir para disco. Toda parte que o parser terminou é
    fechada (e o temporário apagado) no `finally`; uma parte inacabada (corpo truncado/quebrado) é
    do Starlette e sai com o coletor de lixo. Nada é armazenado; nada sobrevive ao request.
12. **Skill `attachment-upload-relay` adiada para a parte 2**, como o prompt dela prevê (registrar
    com as DUAS implementações como referência).
13. **A sessão do banco é fechada logo após o entitlement** (`send_thread_message`,
    `get_thread_media`), antes de qualquer I/O no ritmo do cliente. Achado HIGH da revisão: o
    teardown do `get_session` só roda depois da resposta, então um corpo gotejado (ou upload lento em
    rede móvel, ou download lento) segurava uma conexão do pool; ~15 esgotavam o pool (padrão 5+10)
    e derrubavam todas as rotas do serviço. Nenhuma dessas rotas faz commit.
14. **Sem timeout de leitura de corpo nesta rota.** Com a conexão liberada, um corpo lento custa uma
    corrotina + socket + spool, igual a qualquer rota POST do app (o FastAPI já lia corpos
    declarados antes da autenticação); timeout de leitura é papel do proxy de entrada, e um teto
    apertado aqui quebraria upload legítimo de 20 MiB em 3G. Reverter: `anyio.fail_after` em volta
    de `.body()`/`.form()`.
15. **O codificador multipart do httpx roda numa thread** (`send_attachment`/`_off_the_loop`): o
    httpx lê o arquivo de forma síncrona, e no event loop cada leitura travaria as outras requisições
    (achado HIGH da revisão FastAPI). O corpo continua em stream, uma cópia só, com `Content-Length`.
16. **Erros de leitura de corpo nunca viram 500**: JSON que não é UTF-8, número acima do limite de
    dígitos, aninhamento profundo, cliente que caiu → o mesmo `400` do FastAPI; header de parte
    multipart quebrado → `attachment_malformed`.
17. **Nome de arquivo limpo por CATEGORIA Unicode** (Cc, Cf, Co, Cs, Cn), não por lista: a lista
    deixava passar 7 marcas invisíveis (tag characters, soft hyphen, word joiner...). Isso também
    tirou do código a classe de regex com caracteres invisíveis que, numa etapa, chegou ao disco
    como caracteres LITERAIS (padrão "Trojan Source") — corrigido e varrido nos arquivos tocados.

## 7. Ordem de deploy (`frozen-contract-migration`)

secretarIA parte 2 (API **e** worker) → brain-api (este) → frontend parte 3. Se o brain-api precisar
subir antes por outro motivo: `PATIENT_ATTACHMENTS_ENABLED=false` (uploads recusados com 422; a
projeção da listagem e a rota de mídia ficam inertes até a secretarIA emitir anexos). Sintoma de
ordem errada: upload → `502 product_error` e, no log do brain-api,
`switchboard_upstream_error status=422 path=/internal/brain-message/inbound`.

## 8. Provas

### 8.1 Módulo novo, código final — `uv run python -m pytest tests/test_patient_attachments.py` → **23 passed**

| Item do checklist §5 do prompt | Teste(s) |
|---|---|
| 1. anexo válido (PNG real) aceito, tipo real, repassado em multipart | `test_a_real_image_is_relayed_to_secretaria_as_multipart_typed_by_its_bytes` (asserta o corpo que o httpx pôs no fio, byte a byte, com `Content-Length`) + `test_every_accepted_kind_travels_captionless_as_its_sniffed_type` ×5 |
| 2. PreCheck continua recusando, sem rede | `test_precheck_still_refuses_a_file_and_nothing_is_relayed` |
| 3. `.jpg` que não é imagem / acima do limite / tipo fora da lista | `test_a_file_that_is_not_what_it_claims_is_refused_clearly` ×6, `test_one_byte_over_twenty_mib_is_refused_at_the_real_ceiling` (constante real), `test_the_ceiling_is_inclusive_and_an_oversized_body_is_cut_before_parsing` (inclui corpo chunked), `test_a_malformed_multipart_is_one_clear_refusal`, `test_any_patient_uploads_even_unverified_within_the_budgets_and_the_kill_switch` (visitante não verificado envia; orçamentos e kill switch) |
| 4. mídia só do dono; outro paciente → 404, não 403 | `test_media_is_served_only_inside_the_owners_conversation` (outro paciente, visitante pendente e id inexistente: o MESMO corpo 404; sem sessão → 401), `test_media_refuses_what_it_must_never_serve`, `test_the_transcript_carries_a_reference_never_the_storage` |
| 5. mensagem sem anexo igual a antes | `test_a_message_without_a_file_is_the_same_json_relay_as_before` (+ `tests/test_patient_access.py` inteira verde nas duas rodadas da suíte completa, §8.2) |
| 6. nenhum log com bytes ou nome | `test_no_log_line_carries_the_file_name_or_its_bytes` (stdlib + saída do structlog) |
| 7. pytest + ruff | §8.2 e §8.3 |

### 8.2 Suíte completa — `uv run python -m pytest`

- **Rodada 1 (antes das correções das revisões): 684 passed, 2 skipped, 3 failed** (17 min 41 s).
  As 3 falhas são PRÉ-EXISTENTES: falham igual em HEAD `3ee89b7`, provado rodando as 3 contra um
  `git archive HEAD` com o mesmo ambiente — `test_billing.py::test_checkout_without_stripe_key_returns_503`
  (502≠503), `test_onboarding_endpoints.py::test_get_onboarding_shape_default_state`
  (`embedded_signup` configurado) e `test_precheck_billing.py::test_precheck_internal_usage_event_key_unset_403`
  (401≠403) — todas por valores que o `.env` local define e esses testes supõem ausentes.
- **Rodada 2 (código final — depois das correções das revisões e da abertura do upload a quem não
  verificou o e-mail), rodada pelo dono: 684 passed, 2 skipped, 3 failed** (10 min 17 s): as MESMAS
  3 pré-existentes, com as mesmas asserções. `test_patient_access` (57), `test_patient_account_invites`
  (33), `test_patient_attachments` (23) e `test_patient_pending_session` (28) inteiros verdes. (Uma
  tentativa anterior desta rodada, em background, foi interrompida pelo Claude Code em ~59% por
  falta de memória no sistema — não era falha de teste.)
- Observação pré-existente, fora do escopo: com o `.env` local,
  `test_checkout_without_stripe_key_returns_503` chama a API REAL do Stripe
  (`POST /v1/checkout/sessions`, recusada por preço inexistente) — a suíte não é isolada do `.env`.
  Pendência própria (ex.: o `conftest.py` zerar as chaves de serviços externos), sem editar o `.env`.

### 8.3 ruff

`uv run ruff check` nos 6 arquivos tocados/novos → **All checks passed**; baseline dos 4 que já
existiam, em HEAD (`git show HEAD:<arq> | ruff check --stdin-filename`) → também limpos, então
nenhum achado novo. `ruff format --check` só nos 2 arquivos novos (os existentes não foram
reformatados, regra do repo). Varredura de caracteres invisíveis/controle nos 8 arquivos tocados → 0.

### 8.4 Revisões (subagents) e o que foi feito com cada achado

- `ecc:security-reviewer`: HIGH pool de conexões (§6.13, corrigido); MEDIUM sem cota de bytes (virou
  obrigação da parte 2, §4.1); LOW 500 em corpo ilegível e header multipart quebrado (§6.16,
  corrigido); LOW temporário de corpo truncado só sai no GC (§6.11, documentado); INFO limpeza do
  stream quando o upstream cai no meio (corrigido, `finally` em `_capped`); INFO marcas invisíveis no
  nome (§6.17, corrigido); risco residual: a checagem de dono mora na rota da secretarIA (§4.3).
- `ecc:fastapi-reviewer`: HIGH leitura síncrona do arquivo no event loop (§6.15, corrigido); MEDIUM
  422 diferente do FastAPI para JSON que não é objeto (corrigido — `null` → `missing`, array/escalar
  → `model_attributes_type`); LOW cache de dependência (`get_thread_patient` chama
  `get_thread_identity` direto) — sem mudança: nenhuma rota usa as duas.
- Os scripts de reprodução do próprio revisor, rodados de novo com o código final: requisição
  concorrente durante corpo JSON lento e durante download lento → 200 (antes: `TimeoutError` do
  pool); UTF-8 inválido / inteiro gigante / aninhamento profundo → o mesmo 400 do caminho antigo;
  header de parte quebrado, header gigante e corpo truncado → 422 `attachment_malformed`; ids com
  `%0A`, `%0D%0A`, `%3F`, `%23` e não-ASCII → 422 sem nenhuma chamada ao upstream; upstream caindo no
  meio do stream → `aclose` 1× no cliente e na resposta (antes: 0).
- OpenAPI gera: POST com `application/json` e `multipart/form-data`; rota de mídia com os 5 tipos.

## 9. Pendências

- Arquivos agora podem vir de visitante anônimo: o console da equipe deve tratar todo anexo como não
  confiável (PDF no visualizador do navegador, nunca executado); antivírus fica como opção da parte 2.
- Parte 2 (secretarIA) implementar §4.1–§4.3, **incluindo a cota persistida de bytes e a decisão
  sobre arquivo antes do aceite LGPD** (§4.1); parte
  3 (frontend): CSP (`data:`/`blob:`, com a ressalva do §3.4), nginx `client_max_body_size` com
  margem (≥ 25 MB), `XMLHttpRequest` com progresso.
- Aceito como está (revisões): uma falha do upstream NO MEIO do download aborta a conexão (os headers
  já saíram; o cliente vê erro de rede, não arquivo corrompido) e gera um traceback de `ReadError`
  no log, sem PII; a limpeza da conexão agora roda nesse caminho também.
- Onda 2 como time: `tasks/TASK-003` não foi aberta — esta sessão rodou como Implementer da parte 1
  a partir do prompt colado. Base para o Integrator: `brain-api main@3ee89b7`, checkout normal,
  arquivos em §2 (mais `pyproject.toml`/`uv.lock`). `CLAUDE.md` e
  `docs/CHECKPOINT_portal_sessao_pendente.md` já tinham edições de outra sessão, não desta.
- Adaptação futura do PreCheck = incluir em `ATTACHMENT_PRODUCTS` + o ramo dele em
  `send_attachment`/`open_media`; a validação não muda.
