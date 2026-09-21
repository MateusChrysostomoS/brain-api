# CHECKPOINT — abertura do Portal: e-mail já cadastrado é detectado e volta mascarado (2026-09-21)

Origem: `TECH/BRAIN/z_prompts/PROMPT_BRAIN_MESSAGE_ABERTURA_EMAIL_NOME_1_BRAIN_API.md` (parte 1 de
2 do item 2 da Prioridade 1 de `PLANO_PORTAL_COMO_WHATSAPP.md`). A parte 2 (secretarIA) consome o
contrato descrito no §3 e é quem de fato manda a mensagem ao paciente. Irmão da mesma superfície:
`CHECKPOINT_portal_sessao_pendente.md` (a visita pendente) e `CHECKPOINT_portal_clinicas_convite.md`
(o modelo de conta por convite).

**Estado: IMPLEMENTADO E VALIDADO LOCALMENTE, NÃO COMMITADO, NÃO DEPLOYADO.** Sem migração — nenhuma
coluna nova; a detecção é uma leitura de uma tabela que já existe. Comandos e resultado da validação
no §6.

## §1 — Causa raiz (reconfirmada no código antes de programar)

`services/portal/patient_access.py::claim_pending_email` recebia `(tenant_id, patient_ref, email)`, achava a
`MessagePendingSession` viva da visita e **sobrescrevia incondicionalmente** `row.email =
normalize_email(email)`. Nunca perguntava se aquele endereço já era o de uma `MessagePatientAccount`.
O endpoint `POST /internal/brain-message/pending-email` (`api/portal/internal.py`) devolvia, em consequência,
só `{"status": "claimed"}` — um "registrado" que não distingue quem é novo de quem está voltando.

A consulta que faltava já existia duas vezes no MESMO arquivo — `_ensure_account` (upsert com
`on_conflict_do_nothing`) e a perna não-adotante de `_row_account`
(`select(MessagePatientAccount).where(MessagePatientAccount.email == ...)`) — mas só rodava DEPOIS
que a pessoa já tinha provado o e-mail. Nunca na primeira captura em chat. Nenhuma terceira forma de
consulta foi inventada: a nova reusa a forma de `_row_account`, estreitada para a chave
(`select(MessagePatientAccount.id)`), porque existência é a pergunta inteira e puxar a entidade
colocaria no identity map uma conta que esta request não usa.

## §2 — Decisões desta rodada (fechadas, não reabrir sem o dono)

**1. O endereço continua sendo reivindicado, mesmo quando já pertence a uma conta.** É a mesma visita
capturando o mesmo inbox, e `issue_pending_otp` lê `pending.email` para mandar o código — pular a
escrita para uma conta conhecida quebraria o código exatamente para os pacientes que voltam, que são
os que este ramo existe para servir. O que a existência da conta muda é **a próxima pergunta que a
secretarIA faz**, nada na reivindicação. Provado por
`test_a_known_address_is_still_claimed_and_no_account_is_created_or_linked`.

**2. A consulta acontece ANTES da escrita, dentro da transação que o claim commita.** Assim a
resposta e a reivindicação descrevem UM snapshot: nunca se responde "endereço novo" a partir de uma
leitura feita em transação diferente daquela que escreveu. Uma criação concorrente da mesma conta é
respondida igual nas duas ordens (o perdedor da corrida vê o estado de antes), então a ordem foi
escolhida pela consistência, não por uma proteção de corrida que nenhuma das duas dá.

**3. A detecção só LÊ.** Nada aqui cria, adota ou vincula conta — `_ensure_account`/`_adopt`
continuam atrás de um código provado. É o teste do §6 que segura isso (contagem de contas
inalterada).

**4. A máscara é construída no serviço, não na rota.** `PendingEmailClaim.email_masked` sai de
`core/email_mask.py::mask_email` (o nome real da função; `email_masked`, citado no plano-mãe, não
existe) aplicado ao endereço **normalizado**. Duas consequências deliberadas: nenhuma camada acima do
serviço chega a segurar um endereço cru para mascarar, e a máscara é byte a byte a mesma que
`PendingOtpRequestOut.email_masked` devolve depois para a mesma visita — o paciente não pode ver duas
grafias diferentes do próprio inbox em duas mensagens seguidas.

## §3 — O contrato exato que a parte 2 (secretarIA) vai consumir

`POST /internal/brain-message/pending-email` — request **inalterado**
(`tenant_id`, `external_id`, `email`; `extra="forbid"`, nada novo entra). A resposta 200 ganhou dois
campos, ambos com default:

```json
{ "status": "claimed", "account_exists": false, "email_masked": null }          // e-mail novo
{ "status": "claimed", "account_exists": true,  "email_masked": "p***e@exemplo.com" }  // já é conta
```

- `account_exists: bool` — default `false`. Verdadeiro quando o endereço normalizado já é o de uma
  `MessagePatientAccount`.
- `email_masked: str | None` — default `null`. Presente **só** com `account_exists: true`; não há
  inbox para ninguém reconhecer quando ninguém está sendo convidado a voltar. Forma: primeiro
  caractere + `***` + último caractere do local part + `@` + domínio inteiro.
- 404 (visita desconhecida/morta/clínica errada) e seu `detail` único: **inalterados**.

**Aditivo e seguro nas duas ordens de deploy.** O consumidor de hoje
(`secretarIA/services/pending_identity.py::claim_email`) lê só o **status code** e nunca faz parse do
corpo — verificado nesta rodada, não suposto. Logo: um brain-api novo na frente de uma secretarIA
velha não muda nada, e a parte 2 deve ler os dois campos **best-effort**, tratando a ausência como
"brain-api mais antigo", nunca como erro — a mesma disciplina que `RequestCodeResult.email_masked` já
estabeleceu no sentido contrário. `PendingEmailClaimOut` é modelo de RESPOSTA e não declara
`extra="forbid"` (só os `*In` declaram), então a skill `frozen-contract-migration` se aplica pela
metade aqui: o lado congelado (request) não foi tocado.

Do lado do serviço, `claim_pending_email` passou a devolver o dataclass frozen
`PendingEmailClaim(pending, account_exists, email_masked)` em vez de `MessagePendingSession | None`
— mesma forma de `PatientSessionRenewal`/`PendingIdentityVerification`, que já são o padrão do
arquivo para "o resultado é mais que a linha". `None` continua sendo a única resposta para toda
recusa.

## §4 — Tensão registrada, não decidida em silêncio (`cross-tenant-account-linking`)

`MessagePatientAccount` **não tem `tenant_id`**: é única por e-mail, plataforma inteira
(`models/patient_access.py`, `UniqueConstraint("email")`). Então `account_exists` é um fato de
PLATAFORMA sendo exposto dentro do chat de UMA clínica. Um visitante na clínica A que digita um
endereço cuja conta nasceu só na clínica B ouve "seu e-mail já está no nosso sistema".

Isso **não é** o vínculo automático entre clínicas-irmãs que
`CHECKPOINT_portal_clinicas_convite.md` revogou (o dono tirou `find_sibling_candidates` +
`confirm_sibling_link` do produto): nada é adotado, vinculado ou revelado sobre QUAL clínica — o
único bit é "existe". E o portão continua sendo o código de 6 dígitos: saber que a conta existe não
abre nada.

Mas é, sim, uma divulgação de existência que atravessa a fronteira de tenant, e a decisão de produto
do dono (2026-09-20) foi tomada sem que esse detalhe estivesse na mesa. **Fica registrado para
decisão, não resolvido aqui.** As duas saídas, se o dono quiser estreitar:

1. Escopar a detecção à clínica (`account_exists` só se a conta já tem um `MessagePatient` neste
   `tenant_id`) — custa o benefício principal: quem volta por outra clínica é tratado como novo.
2. Manter como está — o endereço é o do próprio visitante, a máscara descreve o que ele acabou de
   digitar, e a conta é por endereço por desenho desde 2026-09-12.

Relacionado e também aceito explicitamente: como a rota é a perna de serviço
(`X-Internal-Api-Key`), o oráculo de enumeração não é público; mas a secretarIA repassa o bit para o
chat, então na prática quem digita um endereço qualquer no Portal descobre se ele tem conta. É o
pedido literal do dono ("seu e-mail já está no nosso sistema") e está anotado como custo aceito.

## §5 — PII: o que pode e o que não pode sair

O endereço cru **não sai deste serviço** — nem no campo novo, nem em log, nem em detail de erro
(a nota de PII de `api/portal/patient_access.py`, `CHECKPOINT_portal_sessao_pendente.md`). A única forma
permitida continua sendo a de `core/email_mask.py`, e ela agora tem dois pontos de saída em vez de
um (`pending-otp/request` e este claim).

`account_exists` **pode** ir para log: é um fato de uma clínica, não de uma pessoa — não nomeia
inbox nem identifica ninguém. A máscara **não pode**: log não é aviso para paciente, e dois
caracteres mais o domínio é mais do que `tenant_id` precisa para ser útil. As duas linhas de log
tocadas (`patient_pending_email_claimed` no serviço, `pending_email_claimed` na rota) carregam
`tenant_id` + o booleano, nada mais — e existe teste que prova a ausência, não só a intenção.

`frozen-contract-migration` (item 5, "audite o log de todo hop que passa a carregar dado mais
sensível"): a consulta nova liga o endereço como parâmetro de bind, então um traceback cru de
SQLAlchemy poderia carregá-lo. Não é classe nova de exposição — o `UPDATE` que grava
`row.email` na mesma função já ligava o mesmo valor antes desta mudança — mas fica anotado:
quem mexer no tratamento de exceção desta rota herda essa restrição junto.

`tenant-secrets-encryption`: nenhuma credencial nova entra aqui; nada foi cifrado nem decifrado.
A disciplina aplicável dela é a de não logar/retornar o valor cru, e é a mesma que o módulo já
seguia.

## §6 — Validação (comandos e resultado reais)

Arquivos tocados: `src/brain_api/services/portal/patient_access.py`, `src/brain_api/api/portal/internal.py`,
`src/brain_api/schemas/portal/internal.py`, `tests/test_patient_pending_session.py`.

Testes novos, todos em `tests/test_patient_pending_session.py` (bloco final, "The address that
already belongs to an account"):

- `test_a_first_time_address_is_claimed_without_naming_an_account` — e-mail novo devolve exatamente
  `{"status": "claimed", "account_exists": false, "email_masked": null}`. Comportamento de hoje
  intacto.
- `test_an_address_that_already_has_an_account_comes_back_masked` — e-mail que já é de conta devolve
  `account_exists: true` + `p***e@exemplo.com`, e a máscara é idêntica à que
  `pending-otp/request` devolve depois para a mesma visita.
- `test_a_known_address_is_still_claimed_and_no_account_is_created_or_linked` — a visita gravou o
  endereço e `claimed_at`; a contagem de `MessagePatientAccount` não muda (a detecção não cria nem
  vincula).
- `test_the_claim_leaks_neither_the_raw_address_nor_the_mask_into_a_log` — o endereço cru (e só o
  local part) não aparece em `resp.text`; os logs da rota foram gravados e não contêm nem o endereço
  nem `p***e` — com asserção não-vacuosa de que a rota realmente logou.

Um teste PRÉ-EXISTENTE mudou de expectativa, de propósito:
`test_the_handle_is_the_same_id_from_the_first_message_to_the_code` fixava o corpo exato
`{"status": "claimed"}`. Passou a fixar os três campos (com os dois novos no default) — é um
endereço sem conta, então o valor que ele afirma é o mesmo comportamento, só completo.

Recusa (404 por handle desconhecido / clínica errada / visita morta) segue coberta e inalterada por
`test_the_claim_needs_the_internal_key_and_the_right_conversation`, inclusive a asserção de que uma
recusa não escreve nada.

## §7 — O que a parte 2 (secretarIA) precisa fazer com isto

1. Ler `account_exists`/`email_masked` do 200 do claim, **best-effort** (ausente = brain-api antigo,
   nunca erro).
2. `account_exists: true` → pular a pergunta de nome e ir direto ao desafio de código, montando a
   mensagem do dono com `email_masked` (**nunca** com o endereço que ela mesma capturou: a máscara é
   a única forma autorizada, e é ela que bate com a mensagem seguinte do `pending-otp/request`).
3. `account_exists: false` → ramo de paciente novo, com a pergunta de nome estilizada antes do LGPD.
4. Nada aqui decide se a pergunta de nome tardia do fluxo de agendamento some ou vira confirmação
   silenciosa — essa continua sendo a pendência de produto anotada no plano-mãe, do lado da parte 2.

## §8 — Pendências e o que NÃO foi feito

- **Não commitado, não deployado.** Sem migração, então a ordem de deploy é livre: este serviço pode
  subir antes ou depois da secretarIA (§3).
- **Escopo de tenant de `account_exists` em aberto** (§4) — decisão do dono, não bloqueia a parte 2.
- **`ruff format` do repo continua vermelho no HEAD**, inclusive nos dois arquivos tocados, por
  motivos anteriores a esta mudança (`issue_pending_otp` e `pending_otp_is_active` em
  `patient_access.py`; dois helpers de teste em `test_patient_pending_session.py`). Nenhuma linha
  nova entrou nesse débito e nada foi reformatado em massa para "ficar verde" — seria um diff
  enorme e alheio à tarefa. `ruff check` passa limpo nos quatro arquivos.
