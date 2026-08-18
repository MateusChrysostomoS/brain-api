# CHECKPOINT — reset de senha nativo do brain-api

**Rodada:** 2026-08-14 · **Estado:** BUILT + validado (454 testes pytest verdes, 13 deles
novos e específicos deste fluxo) · **NÃO commitado, NÃO deployado, migração `0014` NÃO
rodada em produção.**

Contrato completo: `CONTRACTS.md` §2.6 (+ §6.2 pelas colunas novas). Este doc cobre o
porquê, o que entrou em cada repo e o que falta.

---

## O bug que originou a rodada

Achado ao dividir o `secretarIA-frontend` do `brain-frontend`:

- As telas `app/(SignOut)/esqueci_senha/*` do brain-frontend importavam de `lib/api.ts`,
  que fala com a **API do PreCheck** (`NEXT_PUBLIC_API_URL`) — não com a brain-api.
- A brain-api **não tinha nenhum código de reset de senha**: sua superfície de auth era
  `/token`, `/refresh`, `/logout`, `/me`, os dois exchanges de token e `/set-password`
  (que não é reset — exige sessão autenticada).
- Logo, qualquer usuário que só existe na brain-api (**todo cadastro self-service por
  `/cadastro`**) pedia reset, o PreCheck procurava o e-mail na tabela dele, não achava e —
  pelo padrão anti-enumeração correto que ele já tem — respondia sucesso genérico e não
  enviava nada.

**Falha 100% silenciosa**, no único fluxo cuja função é recuperar acesso. Sem erro em
lugar nenhum: nem no cliente, nem no log, nem para o usuário.

## O que entrou, por repo

### brain-api (o grosso)

- **`models/user.py`** — `reset_token_hash` (`String(64)`, nullable, indexed) e
  `reset_token_expires_at`, espelhando exatamente `invite_token_hash` /
  `invite_token_expires_at`. Mesmo esquema hash-at-rest + uso único + burn na redenção.
  **Sem tabela separada** (o PreCheck tem uma): um reset pendente por usuário é todo o
  requisito, e sobrescrever o hash já invalida o link anterior de graça.
- **`migrations/versions/0014_password_reset.py`** — aditiva, só colunas nullable + índice.
  Mesmo perfil de risco de `0013`. Não tocou `0012` nem `0013`.
- **`config.py`** — `PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 30`. Em **minutos**, não em
  horas como o convite: é o token de vida mais curta do serviço, e 30 min é o que o
  PreCheck usa desde sempre (paridade para quem tem conta nos dois produtos).
- **`schemas/auth.py`** — `MessageOut`, `PasswordResetRequestIn`, `PasswordResetVerifyIn`,
  `PasswordResetConfirmIn`. Shapes **espelhando o PreCheck de propósito** (ver abaixo).
- **`services/auth.py`** — `issue_password_reset_token`, `find_password_reset_user`,
  `complete_password_reset`. Nenhuma delas levanta/loga diferente para e-mail ou token
  desconhecido; devolvem `None` e quem responde é o router.
- **`api/auth.py`** — os 3 endpoints, com `_check_auth_rate_limit` (o mesmo balde por IP do
  `/token` e `/refresh` — este endpoint dispara e-mail para um endereço escolhido pelo
  chamador, é o mais abusável dos dois).

### secretarIA (toque único, autorizado)

- **`services/email.py`** — uma entrada `"password_reset"` em `_TEMPLATES`, com
  `{name}`, `{link}` e `{ttl_minutes}`. Sem ela, `send_notification_email` cairia em
  `transactional_email_unknown_template` — a **mesma falha silenciosa** que este trabalho
  existe para consertar, só que um andar abaixo. Nada mais foi tocado no repo.
- Contrato verificado programaticamente: os placeholders do template e as variáveis que a
  brain-api envia batem exatamente (`link`, `name`, `ttl_minutes`).

### brain-frontend

- **`lib/manage-api.ts`** — `requestPasswordReset` / `verifyResetToken` /
  `confirmPasswordReset` (CALL SITEs #9-#11), sem token de sessão. Nomes iguais aos que
  `lib/api.ts` já exportava, de propósito, para as telas mudarem só o import.
- As 3 telas `esqueci_senha/*` agora importam de `@/lib/manage-api`.
- **`lib/api.ts` NÃO foi apagado** — continua servindo o painel legado `(SignIn)` e as
  chamadas de summary/metrics do admin. Só parou de rotear reset de senha.
- 8 testes novos (124 no total, tsc limpo).

### secretarIA-frontend

- Fluxo **portado do zero** (não existia lá — as telas foram deliberadamente deixadas de
  fora na divisão, justamente porque só existiam quebradas). Rotas `/esqueci_senha`,
  `/esqueci_senha/token`, `/esqueci_senha/atualizar_senha`, mais o link "Esqueci minha
  senha" de volta em `/`. 128 testes, 14 rotas no build.
- Detalhes em `secretarIA-frontend/docs/CHECKPOINT_secretaria_frontend.md`.

## Decisões que valem revisão

1. **Shapes espelham o PreCheck.** Foi o que permitiu que as 3 telas já existentes fossem
   repontadas por troca de import em vez de reescrita.
2. **Um bug latente foi corrigido no caminho:** as telas detectavam 429 por
   `msg.includes("rate limit")`, texto do SlowAPI do PreCheck. A brain-api responde
   `"Too many attempts. Try again in a minute."` — a checagem pararia de casar em
   silêncio, e na tela de token a string em inglês vazaria para a UI em português. Agora
   usam `err instanceof ManageApiError ? err.status : 0`, idioma já padrão no repo.
3. **Timing não é constante** no `/request` (o ramo que acha o usuário grava no banco e
   dispara e-mail; o outro não faz nenhum dos dois). Registrado como limitação conhecida
   em vez de mascarado — a mesma postura do PreCheck. Fechar isso exige escrita fantasma
   + atraso acolchoado.
4. **E-mail malformado devolve `422`, não o `200` genérico.** Não é vazamento: `EmailStr`
   rejeita por formato, antes do handler. Um endereço bem formado e **não cadastrado**
   continua recebendo o mesmo `200` de um real. Há teste fixando isso, com comentário
   explicando, justamente porque *parece* o buraco que o endpoint existe para evitar.
5. **Confirmar um reset NÃO revoga os refresh tokens existentes.** Endurecimento
   defensável (um reset costuma ser disparado *por* um comprometimento), mas não existe
   helper de revogar-tudo-do-usuário hoje e invalidação de sessão merece revisão própria.
   Deixado explicitamente de fora, com comentário no código.

## Pendências

- [ ] Commit + push nos 4 repos (nada foi commitado nesta rodada).
- [ ] **Rodar a migração `0014_password_reset` em produção** (passo manual de alembic — o
      deploy não roda migração sozinho). Aditiva e nullable: segura com o serviço no ar.
- [ ] **Decidir para onde `FRONTEND_BASE_URL` aponta.** A brain-api monta o link de reset
      com a **mesma** env var do link de convite, e agora **os dois** frontends servem
      `/esqueci_senha`. Onde ela apontar é onde todo mundo que pede reset cai. Mesma
      decisão de deploy já rastreada em `CHECKPOINT_secretary_role.md` para o convite —
      resolver as duas juntas, não separar.
- [ ] Teste com e-mail real (SMTP) depois do deploy: os testes locais verificam que
      `send_notification_email` é chamado com o template certo, não que o e-mail chega.
- [ ] Considerar revogar refresh tokens no confirm (decisão 5 acima).
