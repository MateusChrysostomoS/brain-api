# Conta pessoal de equipe — contrato congelado TASK-057

Estado: implementação local em `task/TASK-057-staff-account`; NÃO publicado. Não autoriza fases consumidoras em produção. O checkpoint registra validação e revisão. Somente equipe: paciente e admin continuam em suas populações próprias.

## Identidade, permissões e armazenamento

`StaffAccount` guarda e-mail normalizado (`strip().lower()`), hash pessoal, obrigação/expiração pessoal de primeiro acesso, versão pessoal, estado desabilitado, prova de recuperação e preferência. `User.id` continua sendo o vínculo clínico; `tenant_id`, `professional_id`, papel, proprietário, gestão, pendência e revogação pertencem somente a ele. Não se fundem usuários nem profissionais. A migração adota exclusivamente cada User legado elegível em uma conta própria, com o mesmo UUID; não descobre clínicas por igualdade de atributos. Admin não é adotado. Contas de paciente não são consultadas.

`User.password_hash` permanece como coluna de compatibilidade. Um vínculo novo para conta existente recebe marcador não autenticável, nunca cópia de senha. Uma alteração de senha pessoal inutiliza os hashes legados dos vínculos. A autenticação account-aware de uma conta já adotada sempre usa a credencial pessoal. Retirar a coluna legada é outra migração, não esta tarefa.

`User.credential_version` é a geração clínica (revogação/gestão/restauro futuro); `StaffAccount.credential_version` é a geração pessoal (senha/logout). Nenhuma flag de A viaja para B. Preferência guarda `User.id` no servidor, na conta comprovada. Não há preferência de identidade compartilhada em localStorage. Logout mantém a preferência; mudar/limpar é explícito. Preferência não concede acesso e não escolhe alternativa silenciosamente.

## APIs para a fase 3

Todas as rotas são relativas à base brain-api. Entradas de seleção/preferência/aceite têm `extra="forbid"`; não aceitam tenant, professional, papel, gestão ou proprietário do navegador.

| Método e caminho | Prova/corpo | Resultado |
|---|---|---|
| `POST /auth/account/login` | `{email,password}`, mesma política por bytes e orçamento de login | `AccountResponse`, ou `TokenResponse` com `requires_password_change:true` e escopo restrito de setup |
| `POST /auth/account/resume` | cookie `__Host-staff_account` + `X-Brain-Client:web` | renova a sessão pessoal NO LUGAR (mesmo `sid`/`account_sid`; só o valor do cookie gira) e retorna o mesmo `AccountResponse`. Valor recém-substituído dentro da janela de graça: 200 **sem** `Set-Cookie`. Credencial inválida/replay fora da graça expira cookie e responde 401 — ver "Renovação da sessão pessoal (TASK-065)" |
| `GET /auth/account/memberships` | bearer `account_token` | próprios vínculos, com nome da clínica e status, sem token/payload clínico |
| `POST /auth/account/select` | bearer pessoal + `{user_id,remember?:boolean}` | `TokenResponse` operacional do vínculo ativo; 404 `membership_not_found`, 403 `membership_revoked`, 409 `membership_pending` |
| `PUT /auth/account/preference` | bearer pessoal + `{user_id:UUID|null}` | 204; `null` limpa; vínculo indicado precisa ser próprio e ativo |
| `POST /auth/account/invites/accept` | bearer pessoal + `{token}` | consome convite do destinatário e devolve vínculos; 404 único `membership_invite_not_found` para convite errado/alheio/expirado/reutilizado/obsoleto |
| `POST /auth/account/first-access/complete` | bearer `staff_password_setup` + `{new_password}` | conclui a obrigação pessoal e abre **sessão pessoal**, sem selecionar clínica automaticamente |
| `POST /auth/account/logout` | cookie pessoal e/ou bearer pessoal; bearer clínico ligado à sessão pessoal também pode somente encerrar | sempre 204, sem CSRF; encerra sessões pessoais e credenciais clínicas da conta e limpa cookies de equipe |

Primeiro acesso F5: reutilizar `POST /auth/first-access/resume` com CSRF. Ausência de setup retorna o JSON legado `requires_password_change:false`; depois tentar `/auth/account/resume`. Um setup inválido é recusado/expirado. Cliente novo conclui pelo novo caminho pessoal; caminho legado `/auth/first-access/complete` continua retornando sessão operacional para cliente legado.

Sem preferência: `selection_state:"selection_required"`, mesmo com uma clínica. Preferência ativa: `preferred_active`, com sessão **somente** desse vínculo. Preferência revogada, excluída ou pendente: `preferred_unavailable`, `clinical_session:null`, com vínculos para card/seletor. Nenhum vínculo ativo continua permitindo sessão pessoal e planos; não permite checkout para terceiros nem qualquer operação clínica. Criar clínica/checkout autenticado fica para a fase 4.

Exemplo sanitizado de login pessoal:

```json
{
  "session_kind":"account",
  "account_token":"<somente-em-memoria>",
  "expires_in":1800,
  "email":"pessoa@example.com",
  "name":"Pessoa fictícia",
  "memberships":[
    {"user_id":"10000000-0000-4000-8000-000000000001","invite_id":null,
     "tenant_id":"20000000-0000-4000-8000-000000000001","clinic_name":"Clínica fictícia A","status":"revoked"},
    {"user_id":"10000000-0000-4000-8000-000000000002","invite_id":null,
     "tenant_id":"20000000-0000-4000-8000-000000000002","clinic_name":"Clínica fictícia B","status":"active"}
  ],
  "preferred_user_id":"10000000-0000-4000-8000-000000000001",
  "selection_state":"preferred_unavailable",
  "clinical_session":null
}
```

`status`: `active`, `pending_invite`, `revoked`, `pending_first_access`. A listagem administrativa legada `/doctor/team` representa convite pendente como `pending_first_access`, preservando o enum dos clientes antigos; a listagem pessoal distingue os dois. `invite_id` é reservado e atualmente null; o aceite usa exclusivamente o token entregue ao destinatário.

Texto confirmado para o consumidor, após prova válida: “Seu acesso ao Portal como médico da Clínica {{NM_da_Clinica}} foi revogado”. CTA: “Monte sua própria clínica com nossos planos”. Nome vem somente da listagem pessoal autenticada. Tentativa de login inválida nunca retorna nome/status de clínica.

## Sessão operacional equivalente a Session

`TokenResponse` mantém `access_token`, `token_type`, `refresh_token`, `expires_in`, `name`, `email`, `professional_id`, `requires_password_change`. Campos opcionais aditivos: `user_id`, `tenant_id`, `role`. Cliente mantém a interface existente:

| Session do cliente | Resposta operacional |
|---|---|
| `token` | `access_token` |
| `tenantId` | `tenant_id` (mesmo valor do JWT) |
| `email` | `email` |
| `role` | `role` do vínculo de destino |
| `userId` | `user_id` (JWT `sub`, User.id existente) |

Seleção pessoal retorna `refresh_token:null`. Sua renovação é `/auth/account/resume`, não `/auth/refresh`. JWT clínico não tem scope; `sub` é User.id de destino, com tenant/professional/papel/flags daquele vínculo, versão clínica e `account_sid`. Gates verificam User e sessão pessoal ao vivo. `account_token` tem `scope=staff_account`, `sub=StaffAccount.id`, `sid` e versão pessoal; não passa em staff, hub, billing ou PreCheck. Nenhum dado clínico vai nele. Nova página/F5 não recebe tokens do armazenamento: retoma cookie pessoal e recebe a resposta pessoal.

Tokens pessoais e novos tickets de setup usam chaves HMAC derivadas da SECRET_KEY por propósito, distintas da assinatura operacional compartilhada. PreCheck tem validadores permissivos por sub: somente scope não bastaria. Esses validadores recusam as novas assinaturas sem alteração no irmão. A chave anterior derivada é aceita durante rotação; tickets de setup legados continuam verificáveis apenas pelo fluxo restrito do brain-api, com versão/expiração ao vivo.

### Renovação da sessão pessoal (TASK-065, 2026-10-20)

Mesmo desenho da sessão do paciente (`docs/CHECKPOINT_patient_session_refresh.md`). Detalhes,
provas e ordem de deploy em `docs/CHECKPOINT_staff_session_rotation.md`.

- A linha `StaffAccountSession` mantém o `id` durante toda a vida do login. `resume` gira só o
  valor opaco: hash novo em `token_hash`, o anterior em `previous_token_hash` com `rotated_at`
  (migração `0033_staff_session_rotation`), e `expires_at` desliza para agora +
  `REFRESH_TOKEN_EXPIRE_DAYS` (mesma vida deslizante de antes). Consequência: `account_token` e
  tokens clínicos (`account_sid`) emitidos antes de um `resume` — inclusive em outra aba —
  continuam válidos até a própria expiração curta (`ACCESS_TOKEN_EXPIRE_MINUTES`).
- Valor anterior apresentado em até `STAFF_ACCOUNT_SESSION_ROTATION_GRACE_SECONDS` (padrão 60) depois
  da rotação: 200 com `AccountResponse` da mesma linha e **sem `Set-Cookie`** (o navegador já
  recebeu o sucessor na resposta que girou; escrever outro cookie só criaria corrida).
- O valor do cookie é `<id da linha>.<segredo>` (opaco para o cliente; só o hash do segredo é
  guardado). Qualquer outro segredo apresentado para uma linha da geração viva — o anterior fora da
  janela, um de duas ou mais renovações atrás, lixo — é sinal de reuso: encerra todas as sessões
  pessoais, refresh e tokens clínicos da conta (`end_account_sessions`), 401 com cookie expirado.
  Valor atual de linha revogada com a geração viva: idem, como antes.
- Valor que não nomeia linha (malformado, id inexistente), linha expirada ou de geração antiga: 401
  com cookie expirado, sem encerrar nada.
- Cookie emitido antes da TASK-065 (só o segredo): aceito por hash até a primeira renovação, que já
  devolve o formato novo.
- Concorrência: troca por compare-and-swap (`UPDATE ... WHERE token_hash = <apresentado>`), sob o
  lock da conta; quem perde cai na janela de graça.
- `POST /auth/account/logout` (e o logout legado) usa o mesmo formato e reconhece o segredo atual
  ou o recém-substituído: logout só retira acesso. Segredo errado não encerra nada no logout.
- Logs: `staff_account_session_reuse_detected` e `staff_account_session_grace_accepted`, só com o
  id da sessão.
- O cliente deve manter single-flight por aba; a graça cobre requisições em voo, não um cookie
  velho reapresentado mais de 60 s depois.

Fase 3 deve limpar estado, caches, hub tokens e solicitações atrasadas ao trocar clínica/conta. Deve distinguir sessão operacional legada (renovação legada) da operacional derivada da conta (renovação pessoal). Não reinterpretar `refresh_token:null` como impersonação sem usar essa distinção. Retomada sem preferência retorna o seletor; não reutilizar o contexto anterior por conveniência.

## Convites e credenciais

Gestor do tenant de destino cria o vínculo pendente. Conta existente recebe `staff_membership_invite` pelo mailer do brain-api, token aleatório de 256 bits, hash em repouso, uso único, 72h, vinculado ao destinatário e à geração do vínculo. Nenhuma senha temporária para essa conta. Operação pendente é recusada mesmo para JWT forjado com tenant/profissional do alvo. Aceite exige sessão pessoal viva, revalidada sob lock. Reenvio gira prova/geração do vínculo e preserva Professional.id. Duplicata no tenant retorna 409 estável; convites entre tenants são habilitados somente pela flag de rollout. Profissional é criado no convite autorizado, nunca na seleção ou aceite.

Conta nova mantém senha temporária exclusiva de 72h, setup de no máximo 10min, sem refresh operacional antes de completar. Resend da obrigação pessoal somente enquanto ela existe; não troca senha de conta já estabelecida. Convites legados por link continuam vinculados a um único User e são consumidos atomicamente. Novos convites de conta existente nunca usam essa rota legada para abrir sessão.

Recuperação preserva `/request` → `/verify-code` → `/confirm`, código de seis dígitos, TTL 10min, cinco tentativas, limites configurados e token único de 15min. SMTP continua depois da resposta pública. Conta válida recupera mesmo com todos os vínculos revogados. Migração/adopção preservam hash, expiração, tentativas, janela/cota e reset token legado da própria identidade. A linha pessoal permanece consumida, retendo contadores após sucesso; resend invalida o código anterior. Reset troca a senha pessoal e invalida sessões/refresh/setup antigos em todas as clínicas, sem limpar revogação, conceder gestão/propriedade ou aceitar convites pendentes. A versão clínica de vínculos que podem ter sessão sobe; convites legados autenticadores são consumidos. Convite novo pendente continua pendente, só aceito por nova sessão pessoal comprovada. Revogação/gestão/restauro/reenvio do alvo tornam sua prova obsoleta. Confirmar retorna ao login, sem autenticar com o reset token.

Refresh clínico registra a geração de prova e emissão verifica a geração esperada: login/convite que correr contra reset não pode abrir refresh novo a partir de prova anterior. Cookies preexistentes são backfilled com a geração clínica atual na 0031. A transferência de credencial/desafio/reset ocorre na 0032 ou adoção explícita após ativação segura. Refresh de escritor antigo com geração null em usuário já adotado pede novo login, em vez de adivinhar a prova. Admin legacy não é adotado; suas sessões continuam separadas.

Login, consumo de convite e conclusão de primeiro acesso conservam a geração autorizada até a emissão; uma leitura depois do commit não pode substituí-la pela geração de um reset posterior. A conclusão de setup transporta sua geração resultante esperada, pessoal e clínica. Abertura pessoal revalida essa geração sob lock da conta. Reset entre prova e emissão exige nova autenticação, sem entregar sessão válida.

Refresh expirado ou de geração antiga é recusado antes do efeito destrutivo de replay. Replay atual revoga somente refresh da mesma geração do vínculo. No legado ainda não adotado, null pode renovar uma vez para uma geração explícita; replay daquele null só atinge outros null, nunca seu sucessor versionado. Na conta já adotada, null não autentica. Replay pessoal preserva a mesma separação de gerações. Nenhum desses caminhos restaura revogação ou gestão.

Privacidade administrativa continua protegida pelo gate existente; sessão pessoal não abre essas rotas. Export de equipe acrescenta `account` (nullable) com id/e-mail/nome/preferência/data de criação, sem hashes, códigos ou tokens, inclusive quando não restam vínculos. Apagamento por e-mail remove também a conta independente e suas sessões/desafios/convites. A identificação por e-mail nesta operação administrativa de privacidade não cria ou aceita vínculo clínico.

## Fronteiras e fase 2

Hub recebe `account_sid` quando derivado da sessão pessoal; introspecção revalida sessão, vínculo, versão, profissional e entitlement. Nada mudou nos payloads estritos de secretarIA/handoff; não há novo campo enviado ao contrato de PreCheck. Reset/revogação/logout bloqueiam novos SSO, mas **não encerram sessões nativas PreCheck já emitidas**. JWTs clínicos já apresentados diretamente às rotas Brain do PreCheck também são validados lá sem consultar versão do brain-api, até expirar. Tickets de setup legados assinados pela chave compartilhada têm esse limite pré-existente durante seu TTL curto; novos tickets são recusados por assinatura no irmão. Não corrigimos tokens antigos/paciente nem rotas compartilhadas com WhatsApp. IDs de `PrecheckAccountLink` e Professional permanecem.

Restauro NÃO implementado. Fase 2 deve proteger proprietário/última gestão; restaurar médico sem gestão, em ação separada para gestão; incrementar User.credential_version em toda transição; nunca reutilizar refresh/setup/convite anterior; manter Professional.id, disponibilidade, agendas/subagendas e história; não limpar pendência de convite por restauro; nunca alterar senha/versão pessoal só por revogar/restaurar A. Comparação condicional da geração e locks devem impedir que restauro faça token anterior reviver. Impersonação por e-mail ambíguo é recusada; ferramenta de vínculo PreCheck pede User.id explícito quando há vários vínculos. Scripts não promovem staff a admin por e-mail.

## Migração, matriz e ordem de rollout

Escritores legados `/doctor/professionals/invites` e `/doctor/secretaries/invites`: durante expansão/flag de credencial false conservam o fluxo antigo. Com credencial true delegam ao fluxo canônico de equipe, mantendo campos/IDs, mas `invite_link` aponta para login e a prova temporária/convite vai ao destinatário pelo mailer. Não emitem novo link autenticador que dispense primeiro acesso. Consumidores precisam tolerar essa semântica antes da ativação; links antigos já emitidos mantêm a compatibilidade restrita.

1. Preparar DDL **até 0031**, aditiva após 0030, com `STAFF_ACCOUNT_CREDENTIALS_ENABLED=false` e `STAFF_MULTI_CLINIC_ENABLED=false`. Nenhuma conta/credencial/desafio é adotada ou consumida nesse DDL: senha, códigos, cotas e reset token continuam legados. Colunas novas são nullable/default compatível e unique global permanece. Abertura pessoal está desativada por flag nessa etapa.
2. Colocar **todos** os escritores/leitores (processos API e scripts) na versão account-aware e provar paridade. Cadastros anônimos e primeiro acesso continuam legados enquanto a flag de credencial estiver false. Escritores antigos podem operar durante expansão, mas não após transferência. `adopt_new_user` só cria conta para cadastro novo quando ativado; conta já adotada nunca retorna à senha legada ao desligar flag.
3. Validar fases 2/3 consumidoras compatíveis e drenar solicitações de identidade antes da transferência. Só então aplicar **0032**: adota cada User elegível por seu próprio ID, transfere credencial/provas/counters mais recentes, normaliza e-mails e retira unique global com locks e guardas PostgreSQL. Colisão normalizada/conta ambígua recusa a transação, nunca mescla. Releituras/condições protegem caminhos em voo; no ponto de corte uma prova pode pedir repetição, sem conceder contexto adicional.
4. Habilitar `STAFF_ACCOUNT_CREDENTIALS_ENABLED=true` para login pessoal e novas contas; manter múltiplos vínculos false até aceite integrado. Adoção explícita usa somente o próprio User legado. Só habilitar `STAFF_MULTI_CLINIC_ENABLED=true` após 0032 e consumidores compatíveis. Fase 4 vem depois; nenhum Stripe/checkout nesta fase. Publicação de qualquer etapa NÃO autorizada nesta entrega.

| Cliente/cenário | Código novo + 0031, flag false | 0032 + consumidores compatíveis |
|---|---|---|
| Cliente antigo, uma clínica | login/refresh/setup/reset e campos legados mantidos | mantido enquanto houver uma clínica |
| Cliente antigo, várias clínicas | criação dessa situação bloqueada | login por e-mail recusa contexto ambíguo; usar seletor novo, nunca escolher primeira clínica |
| Cliente novo pessoal | abertura pessoal desativada por flag; testes locais ativam após escritores compatíveis | seletor/preferência/cookie; uma clínica ainda pede seleção; convite depende da segunda flag |
| Cookie/JWT emitido antes da migração | ID e geração preservados; reset/revogação continuam invalidando | idem |
| Paciente/admin | população e gates separados | idem; admin não pode ser associado por atributo |
| Escritor antigo após DDL | senha/prova/cota permanecem legadas na expansão; null não autentica conta já adotada | não autorizado; guardas DB impedem reservar e-mail de outra população |

Rollback seguro é preferencialmente código account-aware com múltiplos vínculos false, mantendo a flag de credencial true quando já usada, compatibilidade clínica e schema. Downgrade 0032 recusa e-mails com múltiplos vínculos. Downgrade 0031 recusa contas sem/excessivamente vinculadas e qualquer convite novo; caso unívoco, devolve credencial/provas/cotas ao único User, aumenta geração, revoga refresh e preserva revogação (inclusive conta desabilitada). Desafio pessoal consumido volta expirado, impedindo replay. Retirar checks de revogação não é rollback. Após multiclínica/contas vazias, rollback pré-conta não é suportado.

## Limites e baseline

Os testes de downgrade comprovam preservação de dados em estado unívoco, não autorização para voltar ao servidor `176c541` sem alterações. Código anterior reintroduziria emissão de setup sob chave compartilhada e não conhece os novos escopos. Depois da ativação, só rollback para código compatível que mantenha assinaturas por propósito, revogação/gerações e isolamento de admins, com schema que ele suporte; manter a versão account-aware é o caminho previsto. Não executar código novo contra schema 0030. Desativar/reabilitar uma conta pessoal futuramente deverá sempre girar geração/revogar suas sessões; nenhum endpoint dessa gestão pessoal foi implementado aqui.

Inbox compartilhada/reutilizada continua sendo risco da conta identificada por e-mail; convites não descobrem/grantem clínicas por esse atributo. Convite tem 256 bits e exige conta comprovada, mas token pessoal vazado alcança os próprios vínculos e convites dirigidos à conta. E-mail de recuperação não é autenticação forte de dois fatores. Logs de operações pessoais não incluem e-mail, nome da clínica nem account id.

F3/F5/F7 da TASK-056 permanecem baseline. F4 permanece no caminho legado não adotado; a nova emissão pessoal serializa na conta para preservar a cota comum sob concorrência, necessidade desta migração. Não foram eliminados jobs/callbacks legados, nem alterados avisos/e-mails operacionais da SecretarIA. Captura SMTP local prova recebimento no capturador, não entrega externa.

## Nota 2026-10-18 — padrões ligados

`STAFF_ACCOUNT_CREDENTIALS_ENABLED` e `STAFF_MULTI_CLINIC_ENABLED` agora têm padrão `true` (decisão do dono: a funcionalidade não depende de variável de ambiente). As variáveis continuam podendo desligar o recurso. `BRAIN_MESSAGE_PUBLIC_URL` tem padrão no frontend implantado (sobrescreva por variável em domínio próprio). Com a chave ligada, os convites legados delegam ao fluxo de equipe: `invite_link` aponta para `/login`. Os testes do fluxo legado (token em `/convite`) rodam com a fixture `legacy_staff_invites`, que desliga a chave.
## Extensão TASK-062 — criar clínica própria (somente local)

Contrato adicional implementado nas worktrees TASK-062, não publicado; fases existentes neste
documento não foram reimplementadas. `POST /auth/account/clinics` exige bearer pessoal e body
estrito com operação UUID/dados de C/catálogo, sem e-mail/senha/tenant/papel. E-mail é o da conta
comprovada; cria Tenant C + User owner/manager ligado à mesma conta + entitlement inerte + intent
numa transação compartilhada com cadastro anônimo. Não copia credenciais/Professional/acessos A/B.

`GET /auth/account/clinics/{operation_id}` e `POST .../checkout` exigem a própria operação e
propriedade C ainda válida. `AccountClinicOut` inclui sessão C com `account_sid`, sem refresh
clínico; cookie pessoal host-only permanece. Sessão clínica antiga, preferência lembrada,
e-mail conhecido ou ID de intent não autorizam criação/faturamento. Todos os portais podem estar
revogados sem impedir a conta de criar C. Revogação posterior C bloqueia a própria retomada.

DDL 0033 após 0032; unique conta/operação e locks; checkout aberto reaproveitado; webhook ativa
somente C. Intents pessoais não entram em checkout/catálogo/cortesia/status/ready/token públicos.
Não se mistura pagamento de C com `/billing` clínico paralelo ou assinatura de A/B. Reset/counters
globais não são reiniciados por clínica nova. Chamadores anônimos novos mantêm o contrato anterior.

PreCheck recebe opcional `brain_user_id` clínico C no provision interno somente para esse intent;
consumidor com migração 0017/guardas nativos deve preceder o emissor. Código antigo não é rollback
seguro após criar esses dados; downgrades recusam população pessoal/mesh. Detalhes, provas e ordem
futura em [checkpoint TASK-062](CHECKPOINT_criar_clinica_conta_existente.md).
