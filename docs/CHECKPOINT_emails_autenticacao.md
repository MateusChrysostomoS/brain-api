# CHECKPOINT — e-mails de autenticação no brain-api + "esqueci a senha" por código (TASK-056)

**Data:** 2026-10-13 · **Estado:** implementado, revisado (`ecc:security-reviewer`) e corrigido;
commitado nas branches `task/TASK-056-*` e integrado à `main` local dos 3 repos.
**NÃO publicado.** Migração `0030` NÃO rodada em produção. Design: `tasks/TASK-056/DESIGN.md`;
revisão: `tasks/TASK-056/REVIEW.md`; registro de execução: `tasks/TASK-056/progress.md`.

Contrato: `CONTRACTS.md` §2.6, §6.2a e §7 (variáveis). Substitui o fluxo por link de
`docs/CHECKPOINT_password_reset.md`.

## O que entrou, por repo

### brain-api
- `services/mailer.py` — envio próprio por SMTP (`smtplib` em thread, STARTTLS **com
  verificação de certificado**, recusa credencial sem TLS, render estrito, CR/LF removido dos
  cabeçalhos, nunca levanta, logs só com o id do modelo). Modelos: `password_reset_code`,
  `staff_temporary_password`, `professional_invite`, `patient_access_otp`, assinados "Equipe
  Brain". Aviso `email_smtp_not_configured` na subida quando `SMTP_HOST` está vazio.
- Os 4 e-mails de autenticação saíram da secretarIA: `services/team.py::deliver_first_access`
  (status `sent`/`failed` gravado na hora, não mais `queued`), os 2 convites de
  `api/onboarding.py`, os 3 pontos do código do paciente em `api/portal/*`. `connection_success`
  continua na secretarIA (é aviso de WhatsApp conectado).
- Envio **depois da resposta** (`BackgroundTasks`) nas rotas públicas: `/auth/password-reset/request`,
  `/patient-access/request-otp`, `/patient-access/pending/request-otp`. O interno
  `/internal/brain-message/pending-otp/request` segue síncrono (responde 503 se o e-mail não sai).
- Recuperação por código: tabela `staff_password_reset_codes` (migração
  `0030_staff_password_reset_codes`, aditiva), `services/auth.py::issue_password_reset_code` /
  `verify_password_reset_code`, rota nova `POST /auth/password-reset/verify-code`, rota
  `/verify` (link) removida, `no-store` nas respostas 200. Regras: 6 dígitos, 10 min, 5
  tentativas, 5 códigos/h por pessoa (persistido), 3 pedidos/min por e-mail, passe de 15 min.

### Brain-Message-Frontend
- `/esqueci-senha/` (`components/console/ForgotPasswordScreen.tsx`): três passos no mesmo
  cartão do login. Link "Esqueci minha senha" no login; volta com aviso "Senha alterada" e
  e-mail preenchido. E-mail e passe só em variáveis de módulo (`lib/real/password-reset.ts`);
  regras puras em `lib/password-reset-flow.ts`.

### brain-frontend
- `/esqueci_senha` → `/esqueci_senha/codigo` (nova) → `/esqueci_senha/atualizar_senha`;
  `/esqueci_senha/token` apagada; o passe não vai mais na URL. Memória em
  `lib/password-reset-memory.ts`; o passe é esquecido ao montar login, passo 1 e passo 2.

### secretarIA / secretarIA-frontend
- Não tocados. **A recuperação de senha do secretarIA-frontend deixa de funcionar** (espera
  um link); portal em desuso, decisão do dono.

## Provas

- Suítes: brain-api `uv run pytest` (linha de base 1446 passed / 3 skipped), Brain-Message
  `npm test` + `tsc` + `build`, brain-frontend `npm test` + `tsc` + `build` — números finais em
  `tasks/TASK-056/TASK.md` → Validation.
- Navegador (local, `next dev` + brain-api em SQLite + caixa SMTP local, sem nenhum serviço
  externo): fluxo completo nos dois frontends, código errado, código com espaço/hífen, senhas
  diferentes, F5 no meio volta ao início, senha antiga 401 / nova 200, nada em
  localStorage/cookie legível/URL, log da API sem e-mail/código/senha, e-mail com remetente
  "Brain". Tema escuro e 375 px no Brain-Message.
- **Não provado:** entrega de e-mail real; migração `0030` num Postgres de verdade (Docker
  desligado — só o SQL offline gerado pelo alembic, aditivo).

## Publicação — o que o dono faz, nesta ordem (cada passo com autorização)

1. No painel, serviço **brain-api**, cadastrar: `SMTP_HOST`, `SMTP_PORT` (587),
   `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS` (true), `EMAIL_FROM_ADDRESS`,
   `EMAIL_FROM_NAME` (Brain). Pode ser a mesma conta SMTP da secretarIA. Porta 465 (TLS
   implícito) **não** funciona — usar 587 com STARTTLS.
2. `alembic upgrade head` (cria `staff_password_reset_codes`) e deploy do brain-api.
3. Teste real: pedir código de recuperação e código de acesso do paciente; ver chegar.
4. Deploy do Brain-Message-Frontend e do brain-frontend.

Sem o passo 1, os 4 e-mails de autenticação **param de sair** (o log avisa).

## Pendências

- Limpeza na secretarIA depois do passo 3 provado: modelos `password_reset`,
  `professional_invite`, `staff_temporary_password`, `patient_access_otp`, o envelope
  `staff_welcome`, o job `send_staff_welcome_email`, `brain_team_delivery`; no brain-api, os
  endpoints `/internal/tenants/{id}/team/{user_id}/delivery`.
- Revisão — achados menores adiados (`tasks/TASK-056/REVIEW.md`): F3 `no-store` também nas
  respostas de erro; F4 lock na contagem por hora; F5 teto diário/bloqueio; F7 contador de
  geração no Brain-Message.
- Roteiro multi-clínica: `staff_password_reset_codes` e `users.reset_token_*` acompanham a
  senha quando ela for para a conta pessoal.
- O revisor não pôde julgar se o proxy de produção sobrescreve `X-Forwarded-For` (decide se o
  limite por IP é contornável; os tetos por conta não dependem disso).
