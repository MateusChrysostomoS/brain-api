# CHECKPOINT — conta única multi-clínica do paciente (Brain-Message) (2026-09-12)

Origem: `TECH/BRAIN/z_prompts/PROMPT_BRAIN_MESSAGE_PORTAL_MULTI_CLINICA_CONTA_SEGURA.md`. Par, ainda
NÃO executado: `PROMPT_BRAIN_MESSAGE_PORTAL_MULTI_CLINICA_FRONTEND.md` (Brain-Message-Frontend), que
recebeu um adendo "Contrato real do brain-api" com o que está no §3 abaixo. Branch
`feature/whatsapp-patient-vision`. Padrão registrado como skill reutilizável:
`TECH/.claude/skills/cross-tenant-account-linking/SKILL.md`.

**Estado: BUILT, UNCOMMITTED, não deployado. Sem migração, sem env nova obrigatória.**
Suíte completa no código final: `598 passed, 12 warnings in 1242.32s (0:20:42)` — HEAD antes da
mudança: `577 passed, 12 warnings` (os mesmos 12 warnings pré-existentes de
`HTTP_422_UNPROCESSABLE_ENTITY` deprecado em rotas antigas).
`tests/test_patient_access.py`: 52 (antes 31). `ruff check` limpo nos arquivos tocados
(`ruff format --check` acusa os mesmos arquivos que já estavam fora de formato no HEAD).
Revisão de segurança (`ecc:security-reviewer`, duas passadas): §6.

## §1 — Ponto de partida (a explicação pro revisor)

A identidade do paciente é isolada por clínica de propósito: `MessagePatient` é único por
`(tenant_id, email)` e o docstring da classe diz que essa é "the tenant isolation boundary in the
data model, before any query ever runs" — o mesmo humano em duas clínicas são duas linhas, dois
handles (`external_id` na secretarIA, `session_ref` no PreCheck) e duas trilhas de consentimento.
(Nota: a frase de LGPD que o prompt cita, "Merging the two identities…", fala no código de juntar
Brain-Message com WhatsApp; o raciocínio vale igual aqui.) O que muda: o e-mail, depois de provado
por código numa clínica, passa a DESCOBRIR as outras clínicas onde já é paciente, e cada uma só vira
sessão depois de uma confirmação que nomeia a clínica, gravada como evento de consentimento. O
trade-off explícito: **provar a caixa de entrada uma vez passa a abrir N clínicas** — quem controla
aquele e-mail (inclusive uma família que o divide, ou quem herdou um endereço reatribuído) vê os
nomes das outras clínicas e pode vinculá-las; a confirmação registra a escolha e evita vínculo
acidental, mas não autentica ninguém além da caixa de entrada.

## §2 — O que mudou

| Onde | O quê |
|---|---|
| `models/patient_access.py` | `CONSENT_KIND_ACCOUNT_LINK = "brain_message_account_link"` + docstrings (módulo, `MessagePatient`, `MessagePatientSession`, `PatientConsentEvent`). **Nenhuma coluna.** |
| `config.py` | `PATIENT_LINK_RATE_LIMIT_PER_MIN = 10`, `PATIENT_LINK_CONFIRM_WINDOW_MINUTES = 30`; comentário de `PATIENT_TOKEN_EXPIRE_MINUTES` corrigido (o JWT deixou de ser irrevogável). |
| `core/security.py` | `create_patient_token(..., session_id, login_session_id=None)` emite `sid` e `login_sid`; `decode_patient_token` exige `sub`, `tenant_id`, `sid`, `login_sid`. |
| `schemas/patient_access.py` | `SiblingCandidateOut {tenant_id, clinic_name, already_linked}`; `PatientSessionOut` + `clinic_name` + `sibling_candidates`; `ConfirmSiblingIn {tenant_id}` (`extra="forbid"`); `ClinicSessionOut {access_token, token_type, expires_in, tenant_id, clinic_name, patient_ref}`. |
| `services/patient_access.py` | `issue_patient_session` devolve `(raw, session_id)` e aceita `lifetime`; novas: `find_live_session`, `session_is_recent`, `find_sibling_candidates` (só leitura), `linked_identities`, `confirm_sibling_link`, `revoke_account_sessions`. |
| `api/patient_access.py` | `_authenticate_patient` (JWT → linha `sid` viva + linha `login_sid` viva + identidade no mesmo tenant); `get_current_patient` (mesma assinatura, rotas de threads intocadas); `get_current_patient_login`; `verify_otp` devolve candidatos; `logout` com Bearer revoga a conta; rota nova `confirm_sibling`; `_link_limiter` (4º balde). |
| `tests/conftest.py` | `PATIENT_LINK_RATE_LIMIT_PER_MIN=0` (como os outros três baldes). |
| `tests/test_patient_access.py` | 21 itens novos (§9) + 3 testes antigos ajustados ao `sid`. |

## §3 — Contrato HTTP (o que o frontend precisa saber)

**`POST /patient-access/verify-otp`** — entrada inalterada; 200 (valores sintéticos):

```json
{
  "access_token": "<JWT>", "token_type": "bearer", "expires_in": 1800,
  "tenant_id": "<uuid A>", "patient_ref": "<uuid>", "clinic_name": "Clínica A",
  "sibling_candidates": [
    {"tenant_id": "<uuid B>", "clinic_name": "Clínica B", "already_linked": false}
  ]
}
```

mais o `Set-Cookie: __Host-patient_session=…` de sempre. Claims do JWT: `sub`, `tenant_id`, `sid`,
`login_sid` (= `sid`), `scope=patient_message`, `iat`, `exp`.

**`POST /patient-access/siblings/{tenant_id}/confirm`**

- Credenciais: `Authorization: Bearer <token do LOGIN>` + o cookie `__Host-patient_session` do
  MESMO login (o client do portal já faz `credentials: "include"`), com login de menos de
  `PATIENT_LINK_CONFIRM_WINDOW_MINUTES`.
- Corpo: `{"tenant_id": "<o mesmo do path>"}`.
- 200 `ClinicSessionOut`, **sem cookie**. O JWT tem `sid` = linha nova (vida de 30 min) e
  `login_sid` = sessão do login.
- 401 `Missing bearer token` / `Invalid or expired token`; 401 `reauthentication_required` (sem
  cookie, cookie de outra sessão, token que não é o do login — ex.: o de uma clínica-irmã —, ou
  login velho) → pedir código de novo.
- 404 `sibling_not_found` — a própria clínica, clínica desconhecida, canal desligado, e-mail que
  nunca verificou lá: a mesma resposta.
- 422 corpo com outro tenant, campo extra ou sem corpo. 429 (`PATIENT_LINK_RATE_LIMIT_PER_MIN` por
  conta, contado só depois das checagens de cookie e de login recente).
- Idempotente: repetir devolve uma sessão nova e não grava outro evento de consentimento.

**`POST /patient-access/logout`** — sempre 200.

- Com Bearer válido de QUALQUER sessão da conta: revoga todas as sessões vivas cujo
  `MessagePatient.email` é o do autenticado (todas as clínicas, todos os aparelhos) e a do cookie.
- Sem Bearer (ou com um morto): só a sessão do cookie — como antes —, e com ela, por `login_sid`,
  os tokens das clínicas abertas a partir daquele login.

**`GET/POST /patient-access/threads/...`** — rotas inalteradas; o que mudou foi a dependência
`get_current_patient`. Um token de clínica-irmã abre só a clínica dele (produtos dela, tenant dela no
relay).

**Quebra intencional:** todo token emitido antes do deploy (sem `sid`) → 401. O portal já pede código
novo a cada reload (não há rota de refresh do paciente), então o custo prático é um login.

## §4 — Decisões tomadas sem perguntar (e dois desvios do texto do prompt)

1. **Desvio 1 — o JWT do paciente ficou revogável (`sid`), e isso toca `get_current_patient`.** O
   passo 4f mandava confirmar que as rotas de threads "NÃO precisam mudar" e parar se precisassem. As
   rotas não mudaram; a dependência que todas usam, sim. Sem isso o passo 4e era falso: "sair revoga a
   conta inteira" revogaria linhas de `message_patient_sessions`, mas cada `access_token` (JWT de 30
   min, sem ligação com linha nenhuma) seguiria abrindo threads até expirar — o que o OWASP ASVS 5.0
   7.4.1 proíbe ("when session termination is triggered (such as logout or expiration), the
   application disallows any further use of the session"). Não é furo do desenho de consentimento
   (descoberta/confirmação continuam como o prompt descreveu); é pré-requisito do 4e, por isso
   resolvido e não parado. Custo: uma leitura por PK por request de paciente (duas para token de
   clínica-irmã) e o re-login do §3.
2. **Desvio 2 — confirmar exige o cookie do próprio login e login recente.** Não estava no prompt. Sem
   isso, um `access_token` vazado (vive na memória da página: XSS, extensão, log de proxy) viraria,
   de qualquer máquina, sessões em TODAS as clínicas-irmãs — e o prompt pede para essa rota "o mesmo
   cuidado" de verify. O cookie `HttpOnly` prova que o pedido vem do navegador que fez o login; a
   janela de 30 min atende o ASVS 5.0 7.5.1 ("requires full re-authentication before allowing
   modifications to sensitive account attributes which may affect authentication") — vincular muda o
   que uma prova de e-mail alcança. Efeitos: token de clínica-irmã não confirma outra (sem
   encadeamento); um segundo login na mesma origem troca o cookie plano e derruba confirmações
   pendentes do primeiro.
3. **O corpo ecoa o tenant do path** (`ConfirmSiblingIn`, 422 se divergir). Path e corpo sem
   conferência seriam duas fontes para a mesma decisão; conferidos, um bug de client vira erro alto em
   vez de vínculo errado.
4. **Um 404 para todo "não é irmã desta conta"**, a própria clínica inclusive. O prompt aceitava
   "404/403"; misturar os dois viraria oráculo de quais clínicas conhecem o endereço.
5. **Idempotência sem GET novo**: `already_linked` em cada candidato (lido de
   `patient_consent_events`) + `confirm` idempotente — uma das duas saídas que o prompt ofereceu. Evita
   uma rota a mais que listaria vínculos.
6. **Sessão de clínica-irmã sem cookie.** `__Host-` exige `Path=/`, então o cookie é plano; um cookie
   por tenant mudaria o contrato do cookie de hoje. O token opaco da linha nova é descartado; a linha
   existe só como alça de revogação do `sid` e **nunca** vai em JSON (seria o leg longo legível por
   JS). Vida da linha = vida do JWT (achado 3, §6).
7. **`login_sid`** (achado 2, §6): o token de clínica-irmã morre com o login de onde saiu. Fecha a
   corrida confirmação × logout e faz o logout sem Bearer derrubar os tokens abertos por aquele login.
   As LINHAS seguem o que o prompt pediu para o fallback: só a do cookie é revogada.
8. **Limite por conta, não por IP** (achado 1, §6): `core/ratelimit.py::client_ip` usa o primeiro
   `X-Forwarded-For`, e atrás do proxy de mesma origem do portal (`Brain-Message-Frontend/nginx.conf`,
   `location /api/brain/`: `proxy_pass` para o hostname PÚBLICO do brain-api, com `X-Forwarded-For`
   anexado — ou seja, a requisição passa duas vezes pelo proxy de entrada do EasyPanel) esse valor
   é forjável ou é o endereço do próprio proxy, conforme o proxy de entrada confie ou não no header. A chave é o e-mail autenticado. O
   prompt pedia "reaproveite o padrão de SlidingWindowLimiter" — mesma classe, mesmo fail-open, mesmo
   teste por monkeypatch; mudou só a chave. E o balde só é consumido depois das checagens de
   cookie e de login recente (achado 5, §6): um Bearer vazado, sem o cookie, não gasta o
   orçamento do paciente.
9. **Logout por e-mail, com o Bearer resolvido ANTES de revogar o cookie.** Na maioria dos casos os
   dois apontam para a mesma linha; revogar primeiro faria o Bearer parecer morto e encolheria o logout
   de conta para logout de aparelho. O join por e-mail pega também sessões do endereço em clínicas que
   esta conta nunca confirmou — qualquer uma delas o dono do e-mail abriria de qualquer jeito.
10. **Logs**: `patient_sibling_candidates_found` (tenant do login + `count`),
    `patient_account_link_confirmed` (tenant vinculado + `consent_events_recorded` 0/1) e
    `patient_account_sessions_revoked` (tenant + `count`). Ao contrário de `patient_otp_verified`,
    **sem `patient_ref`**: uma linha com os handles de duas clínicas para o mesmo humano é justamente o
    join entre tenants que as linhas separadas evitam. Testado com um gravador no lugar do `logger`
    (structlog imprime via `PrintLoggerFactory`; `caplog` não vê esses eventos).
11. **`last_seen_at` da identidade vinculada é carimbado na confirmação** — é um login naquela
    clínica, sem código — e alimenta a mitigação "corte por inatividade" do §8.
12. **Base legal**: o evento de vínculo usa o mesmo `TODO_LAWYER` do evento de acesso ao canal — a
    mesma pendência jurídica, não uma base inventada.
13. **Skill criada sem o ciclo de evals do `skill-creator`.** O prompt pediu a skill para registrar o
    padrão, não para otimizar gatilho; baselines com subagentes custariam mais que a própria skill e não
    mudariam o conteúdo. A descrição é "insistente" de propósito (conta única, multi-clínica, vincular
    contas, troca de clínica de profissional).

## §5 — Por que confirmação explícita, e não vínculo por e-mail

- Pesquisa que originou o prompt, sobre padrões de account-linking: um e-mail normalizado não deve,
  sozinho, autorizar o vínculo, "as an email can be reassigned, an address can be shared".
- OpenID Connect Core 1.0 §5.7 (Claim Stability and Uniqueness): só `iss` + `sub` são identificador
  estável; "an Issuer MAY re-use an email Claim Value across different End-Users at different points in
  time, and the claimed email address for a given End-User MAY change over time".
- Tradução no código: o e-mail só entra como chave de BUSCA a partir de uma linha já provada
  (`find_sibling_candidates`, `confirm_sibling_link`, `revoke_account_sessions`); o que concede acesso
  é o ato nomeado, gravado em `patient_consent_events` na clínica vinculada, antes de qualquer token.

## §6 — Revisão de segurança

Primeira passada (`ecc:security-reviewer`, Opus, diff inteiro): nenhum achado crítico/alto.

| # | Achado | Sev. | Veredito | Tratamento |
|---|---|---|---|---|
| 1 | Limitadores por IP confiam no 1º `X-Forwarded-For`; no caminho portal → nginx → hostname público → brain-api o valor é forjável OU colapsa num balde só | Média | PLAUSIBLE (config do proxy do EasyPanel não verificável daqui) | Rota nova: chave por conta (§4.8) + teste com `X-Forwarded-For` forjado. `request-otp`/`verify-otp` (e `auth`, `signup`, `waitlist`): **pré-existente, fora do escopo** — pendência §10. |
| 2 | Confirmação que corre contra um logout de conta cria sessão que sobrevive a ele (o `UPDATE` do logout não vê a linha inserida depois) | Baixa | CONFIRMED | **Corrigido**: `login_sid` (§4.7). `test_a_link_that_races_a_logout_dies_with_the_login` reproduz a intercalação (logout em outra sessão entre o vínculo e o insert) e prova 401 no primeiro uso. |
| 3 | Cada confirmação insere uma linha viva de 30 dias | Baixa | CONFIRMED | **Corrigido**: a linha vinculada vive `PATIENT_TOKEN_EXPIRE_MINUTES`; limite por conta. Linhas expiradas não são apagadas — igual às de login hoje (sem sweeper, §10). |
| 4 | Access log do uvicorn junta IP + tenant da irmã (está no path); o engine sem `hide_parameters` imprimiria o e-mail num traceback | Baixa | PLAUSIBLE | **Aceito**: quem lê esse log é a plataforma, que já tem no banco os dois `MessagePatient` com o mesmo e-mail; o path é o do contrato pedido. `hide_parameters=True` é mudança global do engine → §10. |

Segunda passada (`ecc:security-reviewer`, Opus, focada nas correções): os achados 2 e 3 foram
**confirmados como corrigidos** — a intercalação percorrida no código, em todas as ordens (logout
antes da autenticação → 401 no confirm; entre a autenticação e o insert → token 401 no primeiro uso;
depois do insert → o `UPDATE` do logout pega a linha nova), e o teste da corrida reproduz a
intercalação (o 401 só pode vir da checagem de `login_sid`). Novos:

| # | Achado | Sev. | Veredito | Tratamento |
|---|---|---|---|---|
| 5 | Qualquer token vivo da conta, SEM o cookie, gastava o balde de vínculo (o `allow()` rodava antes da checagem de cookie) → paciente impedido de vincular por até ~30 min | Baixa | CONFIRMED | **Corrigido**: o balde só conta depois do cookie + login recente; `test_a_bearer_without_the_cookie_cannot_spend_the_link_budget`. |
| 6 | `create_patient_token` caía em `login_sid = sid` sem o parâmetro — um ponto de emissão futuro de token vinculado que o esquecesse sobreviveria ao login | Info | endurecimento | **Corrigido**: parâmetro obrigatório nos dois pontos de emissão. |
| 7 | `lifetime or 30 dias` transformava `timedelta(0)` em 30 dias | Info | detalhe | **Corrigido**: `is None`. |
| 8 | `_authenticate_patient` não confere que a linha `login_sid` é do mesmo e-mail | Info | inalcançável (exige a `SECRET_KEY`) | **Aceito**: o único ponto que emite `login_sid ≠ sid` usa a linha do próprio Bearer, já casada com o cookie; conferir custaria mais uma leitura por request de clínica-irmã. |
| 9 | Uma confirmação que perde a corrida ainda grava o evento de consentimento e deixa uma linha viva de 30 min | Info | não é acesso | **Aceito**: o paciente de fato confirmou; a linha não abre nada (token morto por `login_sid`, token opaco descartado). |
| 10 | Depois de um logout só-cookie, mandar o token vinculado (já morto) ao logout vira logout de aparelho (200); outros logins do endereço seguem vivos | Info | não é regressão | **Aceito**: nada morto revive; para encerrar a conta o client manda um Bearer vivo (adendo do prompt do frontend). |

Nenhum achado CONFIRMED ficou sem correção; os PLAUSIBLE/Info aceitos estão justificados acima e o
item 1 segue como pendência pré-existente (§10).

## §7 — Registro de risco aceito: e-mail OTP e NIST SP 800-63B-4

O `brain-api` usa código por e-mail como **único** fator do paciente do Brain-Message. Sob a NIST SP
800-63B-4 (Revisão 4, versão final publicada em 2025 — o prompt diz 2026) isso **não atende AAL2, e a
rigor e-mail nem é autenticador out-of-band aceito**: a §3.1.3.1 diz "Email SHALL NOT be used for
out-of-band authentication" (acesso só com senha, interceptação em trânsito ou em servidores
intermediários, redirecionamento), e a §2.2 exige que um verificador AAL2 "SHALL offer at least one
phishing-resistant authentication option". Código digitado à mão não resiste a phishing/AiTM: um
proxy malicioso que retransmite o código autentica o atacante tão bem quanto o paciente. **Este
parágrafo é um registro de risco aceito, não uma reivindicação de conformidade.**

Por que aceito: é o canal de conversa com a clínica (agendamento, pré-consulta), não controle
financeiro; o paciente não tem cadastro prévio nem dispositivo gerenciado, então passkey/FIDO2 seria
desproporcional agora (decisão do prompt).

Controles compensatórios que existem no código (conferidos nesta sessão):

- expiração curta: `PATIENT_OTP_EXPIRE_MINUTES = 10`;
- teto por desafio: `PATIENT_OTP_MAX_ATTEMPTS = 5`, com o erro contado e persistido;
- uso único (`consumed_at`) e um desafio vivo por par (reemitir sobrescreve e zera as tentativas);
- rate limit em três dimensões: `_ip_limiter` (5/min por IP, `request-otp`), `_email_limiter` (3/min
  por endereço, `request-otp`), `_verify_limiter` (10/min por IP, `verify-otp`) — com a ressalva do §6
  item 1 sobre os dois por IP;
- resposta idêntica anti-enumeração: `request-otp` sempre o mesmo 200; `verify-otp` o mesmo 400 para
  código errado, expirado, usado ou inexistente;
- código só como SHA-256 no banco, comparado com `secrets.compare_digest`, nunca logado (há teste que lê
  a saída de log);
- desta rodada: sessão revogável de ponta a ponta (`sid`/`login_sid`), vínculo só por confirmação
  nomeada com cookie do login + login recente, logout da conta inteira.

O que a conta única muda nesse risco, dito e não escondido: antes, adivinhar o código de um endereço
abria UMA clínica e exigia saber o id dela; agora qualquer clínica com canal ligado serve de porta de
entrada, e o acerto alcança todas as clínicas onde o endereço já é paciente. Ordem de grandeza contra
um endereço-alvo, se os baldes por IP forem contornados: 3 desafios/min × 5 tentativas = 15
palpites/min ≈ 21.600/dia num espaço de 10⁶ ≈ **2% ao dia**. Mitigações baratas estão no §10, para o
dono decidir.

## §8 — Riscos residuais de desenho (não são bugs; decisões em aberto para o dono)

1. **E-mail compartilhado ou reatribuído**: a confirmação registra a escolha e evita vínculo acidental,
   mas não autentica além da caixa de entrada.
2. **Nomes de clínicas aparecem para quem tem a caixa** antes de qualquer confirmação — inerente à
   pergunta nomeada. O nome de uma clínica pode indicar condição de saúde (dado sensível na LGPD).
   Mitigações possíveis: opt-out por clínica; não oferecer candidatos com `last_seen_at` antigo.
3. **`already_linked` deixa o próximo detentor da caixa pular a pergunta** (decisão do prompt: não
   re-perguntar). Opções: sempre perguntar; expirar o "já confirmado" depois de N dias; registrar qual
   login confirmou (exige coluna → migração).
4. **XSS no portal alcança mais**: todas as clínicas vinculáveis nos 30 min do login. O vínculo ao cookie
   não para script na mesma página, e a CSP do portal permite `'unsafe-inline'` em `script-src`
   (`Brain-Message-Frontend/nginx.conf`, deliberado pela hidratação do App Router).
5. **Logout por e-mail derruba também quem divide o endereço** — consequência de "a conta é o e-mail".
6. **As linhas da clínica B** (evento de vínculo, `last_seen_at`) refletem um login feito em A; nenhuma
   rota expõe isso hoje.
7. **Ainda não existe**: "desvincular" (retirar o consentimento) e índice em `message_patients.email`
   (a busca por e-mail varre a tabela: `uq_message_patients_tenant_email` começa por `tenant_id`).

## §9 — Testes novos (`tests/test_patient_access.py`)

- `test_a_patient_token_without_its_session_claims_is_refused[sid|login_sid]` — token sem a alça de
  revogação não abre nada.
- `test_verify_otp_names_a_sibling_clinic_without_its_token` — duas clínicas abertas, mesmo e-mail: a B
  aparece com as chaves exatas (sem token), nenhuma sessão nem vínculo criados.
- `test_a_clinic_with_the_channel_off_is_never_a_candidate` — nem candidata, nem confirmável (404).
- `test_a_clinic_the_address_never_verified_at_is_never_a_candidate` — nem candidata, nem confirmável,
  e nenhuma identidade criada.
- `test_confirm_without_a_matching_candidate_is_refused[another_address|own_clinic|unknown_clinic]` —
  404, nada gravado, nada emitido.
- `test_confirm_opens_the_existing_identity_and_records_the_link_once` — handle que já existia, um
  evento só em duas confirmações, `login_sid`, linha de 30 min, `already_linked: true` no login seguinte.
- `test_a_sibling_token_opens_that_clinic_and_only_that_clinic` — threads e relay só da B, produtos da
  B, token com tenant re-assinado → 401.
- `test_logout_with_a_bearer_ends_every_session_of_the_account` — as três sessões da conta revogadas,
  outra pessoa na mesma clínica intacta, todo token da conta → 401.
- `test_logout_without_a_bearer_still_ends_only_the_cookie_session` — só a linha do cookie; o token
  vinculado morre por `login_sid`; o outro login do endereço segue vivo.
- `test_a_link_that_races_a_logout_dies_with_the_login` — o achado 2 reproduzido e fechado.
- `test_confirm_needs_the_login_cookie_not_just_a_bearer` — outro navegador → 401; Bearer que não é o
  do cookie → 401.
- `test_confirm_needs_a_recent_login` — login fora da janela → 401.
- `test_confirm_body_must_name_the_same_clinic_and_nothing_else[another_clinic|extra_field|no_body]` —
  422, nada gravado.
- `test_confirm_rate_limit_is_per_account_not_per_ip` — 429, e `X-Forwarded-For` forjado não reseta.
- `test_a_bearer_without_the_cookie_cannot_spend_the_link_budget` — o achado 5 reproduzido e fechado.
- `test_discovery_and_linking_log_tenants_and_counts_only` — campos exatos; nenhum e-mail ou nome de
  clínica.

Ajustados: o caminho feliz (`sid` ligado à linha do cookie, `clinic_name`, lista vazia), o token forjado
com outro tenant (agora com `sid` real) e o logout antigo (o `access_token` também morre).

## §10 — Deploy e pendências

- **Ordem**: brain-api primeiro — o corpo só ganhou campos, e o portal atual os ignora (tokens
  pré-deploy → 401 → novo código). Depois o frontend (`PROMPT_…_FRONTEND.md`, já com o adendo de
  contrato). Commit é decisão do dono; `git add` só com caminhos explícitos (sessões paralelas).
- **Fora do escopo, recomendado**: (a) `client_ip` confiável (hop fixo da direita ou header do proxy
  externo) para TODOS os limitadores; (b) orçamento diário de falhas de OTP por endereço; (c) aviso por
  e-mail no primeiro vínculo (template novo na secretarIA); (d) rota de desvincular; (e) índice em
  `message_patients(email)` (migração); (f) `hide_parameters=True` no engine; (g) limpeza de sessões
  expiradas.
