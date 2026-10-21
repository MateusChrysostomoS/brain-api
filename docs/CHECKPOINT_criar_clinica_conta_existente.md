# TASK-062 — Nova clínica de uma conta pessoal existente

Estado: implementação **somente local**, branch `task/TASK-062-criar-clinica`, base
`4768a7525a9338dd2acbb02d793fccbad6029e6d`. Sem commit, merge, push, deploy, SQL remoto ou cobrança real.
Worktree: `C:/TECH/BRAIN-worktrees/TASK-062/brain-api`. Fases 057/058/059 reutilizadas, não refeitas.
Roteiro, bases dos quatro repos e comandos: `C:/TECH/BRAIN/tasks/TASK-062/TASK.md`.

## Contrato e autorização

- `POST /auth/account/clinics`: bearer **pessoal**, body estrito `operation_id` UUID,
  `clinic_name`, `whatsapp_phone`, `catalog_ids`, opcionais `intake`/`precheck_template_slug`.
  E-mail/nome vêm da conta comprovada; não aceita senha, e-mail, tenant ou papéis do cliente.
- Transação compartilhada com o signup anônimo: Tenant C, User proprietário/gestor C,
  vínculo à mesma StaffAccount, entitlement free/inativo e SignupIntent pessoal. Não copia
  senha nem Professional, Google, agenda ou dados de A/B. A conta pode ter zero vínculos ativos.
- `GET /auth/account/clinics/{operation_id}` retoma apenas a própria operação. Criação/retomada
  retornam `AccountClinicOut`, incluindo sessão clínica C (`refresh_token=null`, `account_sid`
  pessoal preservado). Não escolhem preferência lembrada nem renovam o acesso a A/B.
- `POST .../{operation_id}/checkout` exige a mesma prova pessoal e vínculo proprietário C
  ainda válido. JWT clínico, inclusive de C, não cria clínica. Conta estrangeira não lê/paga
  operação alheia; revogação posterior de C também bloqueia retomada/checkout.
- Respostas novas `Cache-Control: no-store`. ID de operação/intent não é autorização.
  Superfícies públicas de checkout, catálogo, cortesia, onboarding status/ready e troca de
  onboarding token recusam intents pessoais. Cadastro anônimo novo permanece compatível;
  e-mail conhecido continua 409, inclusive sob corrida de unicidade.

## Migração e cobrança

`0034_account_clinic_signup`, após `0033_staff_session_rotation` (TASK-065): conta dona, operação,
hash do body, tentativa/URL de checkout; unique `(owner_account_id, operation_id)`.
Aplicada apenas nos PostgreSQL descartáveis locais. Downgrade recusa qualquer intent pessoal:
remover autorização por conta e voltar a tratá-lo como público não é rollback seguro.

Lock da conta serializa create/reset e dupla criação; corpo igual/operação igual retorna C,
corpo alterado retorna 409. Lock do intent serializa checkout/webhook. Uma sessão Stripe aberta
é reutilizada; expirada inicia outra tentativa **do mesmo C**. Falha permanente conhecida sem
subscription (`subscription_create_failed`/`stripe_customer_missing`) pode retomar. Falha de
transporte mantém a chave idempotente; sessão já completa em processamento não abre outra.

Checkout usa setup/customer próprio de C e chave `signup-checkout-<intent>-<attempt>`;
subscription mantém a ponte existente `signup-sub-<intent>`. Nenhuma assinatura de A/B muda.
Compra clínica comum é bloqueada enquanto esse intent pessoal está pendente/falho, evitando
dois caminhos para a mesma contratação. Guard de assinatura ativa continua valendo.
Webhooks de sessão antiga são ignorados; advisory lock por evento PostgreSQL evita corrida
na deduplicação. Assinatura ativa não relacionada a este intent é preservada. Uma assinatura
do próprio intent já registrada por outro webhook é conferida no Stripe e reconciliada sem
nova cobrança. Entitlement só ativa pela ponte de pagamento existente, nunca pela criação.

## Consumidor PreCheck (ampliação autorizada em 2026-10-20)

Reproduzido no código nativo: mesmo e-mail em A impedia provisionar C com 409. O simulador
inicial dos spokes **não provava** esse caso. Agora a worktree PreCheck recebe campo opcional
`brain_user_id` (UUID do **User clínico C**, não StaffAccount) somente no caminho de intent
pessoal. Cria identidade mesh-only separada em C, sem mover/reatribuir médico legado por e-mail.
O link SSO continua por ID explícito e tenant. Cadastro anônimo/admin antigo omite o campo.
Ver `../../PreCheck/docs/CHECKPOINT_criar_clinica_conta_existente.md`.

## Evidências locais

- Baseline signup: 48 passed; novas rotas primeiro falharam 404. Corrida anônima e corrida
  de webhook exato também tiveram reprodução RED antes da correção.
- Signup/billing focados: 74 passed. Suíte completa anterior: 1508 passed, 25 skipped
  (1901,73 s); cobre versão anterior à ponte explícita PreCheck e últimos guardas de retry.
  Execução completa final: **1513 passed, 31 skipped, 39 warnings** (2227,75 s), depois da
  ponte/guardas. Caso adicional de privacidade/revogação posterior foi incluído após a coleta
  dessa rodada e aprovado na rodada focada final: **26 passed** (pessoais/PG/bridge PreCheck).
- Matriz C SQLite, privacidade/revogação posterior e PostgreSQL com conexões independentes:
  testes `test_account_clinic_signup.py` / `test_account_clinic_postgres.py`.
- HTTP real API + PostgreSQL + Stripe HTTP simulado + **PreCheck nativo local**: 6 passed.
  Médico A, A+B, gestor A, todos revogados, zero vínculos e cadastro anônimo novo. SSO `/me`
  do PreCheck aponta para tenant C; credencial/reset counters/professional IDs e cobrança A/B
  preservados. Transporte/Stripe real, SMTP externo e Google não foram usados.
- Browser: card revogado → planos → login pessoal/recuperação por código → C inerte →
  cancelar/F5 → mesma sessão de checkout → pagamento simulado repetido → C no console.
  Repetido com mesmo e-mail já presente em A no PreCheck nativo. Reset F5/Voltar não
  ressuscita autorização; senha antiga falha, nova vale nos dois frontends e A/B seguem revogadas.

## Ordem futura (não autorizada nesta tarefa)

1. DDL 0033 (TASK-065) e 0034 da API e 0017 do PreCheck em ambiente controlado, sem novos emissores ainda.
2. PreCheck consumidor com guardas de login/reset nativos e identidade explícita.
3. API emissora/guardas de intent pessoal; então brain-frontend e console.
4. Provar compatibilidade antes de qualquer ativação real. Não reverter a código sem guardas
   após criar intents pessoais/identidades mesh-only; manter schema e código account-aware.

WhatsApp/n8n, produção e entrega externa de e-mails não foram alterados nem provados.
Calendário/subagendas continuam sendo configurados pelos fluxos existentes; compra não torna
o proprietário agendável automaticamente. Esta tarefa não resolve incidentes de e-mail/worker.
