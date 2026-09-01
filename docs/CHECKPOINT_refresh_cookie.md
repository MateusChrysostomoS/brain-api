# CHECKPOINT — Refresh token em cookie httpOnly `__Host-` + guarda CSRF

**Estado:** BUILT, TESTADO e COMMITADO em 2026-09-01 (`80989fa`). **NÃO deployado.**
**Aditivo de propósito** — nada quebra pra quem já está no ar.

Esta é a **fase 1 de 3** de uma rodada cross-repo (brain-api → secretarIA-frontend →
brain-frontend). O documento completo — motivação, arquitetura, armadilhas, ordem de deploy
e checklist pós-deploy — vive em
`TECH/BRAIN/secretarIA-frontend/docs/CHECKPOINT_sessao_cookie_httponly.md`. Aqui fica só o
lado do backend. Contrato: **CONTRACTS.md §2.1a / §2.1b / §2.1c**.

---

## O que entrou

`src/brain_api/core/cookies.py` (novo) é a fonte única dos atributos do cookie e da guarda
CSRF. Toda rota que cunha par de sessão passa por `api/auth.py::issue_session`, que monta a
resposta **e** planta o cookie — é isso que impede a rotação de deixar o navegador com o
token que o servidor acabou de revogar.

```
Set-Cookie: __Host-refresh_token=<opaco>; Max-Age=1209600; Path=/; Secure; HttpOnly; SameSite=Lax
```

| rota | o que mudou |
| --- | --- |
| `POST /auth/token` | além do corpo, planta o cookie |
| `POST /auth/refresh` | aceita o token do **cookie** ou do corpo (cookie ganha); exige `X-Brain-Client: web` quando a credencial é o cookie, **antes** de rotacionar; um cookie rejeitado é expirado na resposta |
| `POST /auth/logout` | revoga as **duas** pernas quando ambas vêm; sempre 204; sempre expira o cookie; **sem** checagem de header (de propósito — §abaixo) |
| `POST /auth/exchange-onboarding-token`, `/auth/exchange-invite-token` | plantam o cookie |
| `POST /public/signup-intents` | planta o cookie (o primeiro card do wizard **é** um login) |
| `POST /admin/impersonate/token` | **não encosta no cookie** — ver armadilha abaixo |

`TokenResponse` ganhou `email` (aditivo). Um portal que retoma a sessão pelo cookie nunca viu
formulário de login, e o access token não carrega claim de e-mail por design.

`REFRESH_COOKIE_PERSISTENT` (novo, default `true`): `false` emite um **cookie de sessão** (sem
`Max-Age`), que morre com o navegador — o certo pra recepção de clínica com máquina
compartilhada. O token do servidor mantém TTL e revogabilidade dos dois jeitos.

## Três decisões que parecem detalhe e não são

**`SameSite=Lax`, nunca `None`.** `None` só seria necessário enquanto o browser visse a
brain-api como terceiro. A saída foi parar de ser: cada frontend faz proxy reverso desta API
sob a própria origem em `/api/*`. Isso também é o que impede o ITP do Safari e o ETP do
Firefox de despejarem o cookie no meio da sessão — a falha silenciosa que a versão ingênua
teria shipado.

**A guarda CSRF é a camada que sustenta, não a reserva.** `SameSite` compara domínios
registráveis, e este serviço não pode assumir que `easypanel.host` está na Public Suffix
List — se não estiver, um vizinho sob aquele pai conta como same-site. `X-Brain-Client: web`
fecha isso: `<form>` cross-site não seta header, e `fetch()` cross-site que tente cai no
preflight de CORS. Exigida **só** onde o cookie é a credencial; exigir em toda rota mutante
quebraria o webhook da Stripe, os chamadores da malha e o frontend não migrado, sem ganho
nenhum (bearer não é credencial ambiente).

**Nada de header no `/auth/logout`.** O pior que um logout forjado faz é deslogar. Exigir o
header ali compraria isso de volta com uma falha estritamente pior: um 403 deixa o cookie
vivo depois de o portal já ter largado a sessão em memória, e o próximo reload religa o
usuário. Uma rota cujo modo de falha é "continua logado" não pode poder falhar.

## Armadilha: "Modo médico" trocaria de identidade em silêncio

`POST /admin/impersonate/token` cunha um access token **sem perna de refresh**, e o cookie do
navegador continua sendo o do **admin**. Se essa rota escrevesse (ou apenas deixasse) o
cookie e o cliente renovasse no primeiro 401, a resposta seria uma sessão de ADMIN com a
identidade do médico — sem nada mudando na tela. A rota fica fora de `issue_session` de
propósito, e o cliente marca a sessão `refreshable: false`. Há teste pros dois lados.

## Detalhe de FastAPI que custa uma sessão

O `Response` injetado é **descartado quando a rota faz RAISE** — `HTTPException` é renderizada
por um handler que nunca o vê. Expirar o cookie num 401 de refresh rejeitado precisou ir por
`HTTPException(headers=...)`, com o header montado a partir de um `Response` descartável, pra
manter `core/cookies.py` como fonte única: os atributos precisam bater **exatamente** ou o
browser trata o delete como outro cookie e mantém o original.

## Gates

**534 testes verdes**, 20 deles novos em `tests/test_refresh_cookie.py` (emissão e atributos,
refresh só por cookie, precedência sobre o corpo, a perna legada do corpo intacta, 403 sem o
header, o 403 não gasta o token nem apaga o cookie, cookie morto é expirado, rejeição do
corpo não encosta no cookie, logout pelas duas pernas, cadastro planta o cookie,
impersonation não planta).

Eles afirmam sobre a **string `Set-Cookie` crua** de propósito: o cookie jar do httpx respeita
o `Secure` e o client fala `http://test`, então o cookie sob teste seria descartado em
silêncio — e os atributos **são** a propriedade de segurança.

`make lint` **não roda nesta máquina**: o binário do `ruff` é bloqueado pelo Controle de
Aplicativos do Windows. `line-length = 100` e a ordem de import do isort foram conferidos à
mão.

## Pendências

- **Deploy** — esta fase precisa estar no ar e estável antes de qualquer frontend depender
  dela. Nada muda pra quem já está no ar: um `Set-Cookie` numa resposta cross-origin lida com
  `credentials` != `include` é **ignorado** pelo browser.
- **Remover a perna do corpo JSON** (`refresh_token` na resposta e nos bodies) — sessão futura
  separada, só depois dos dois frontends confirmados estáveis em produção.
- **CORS não foi tocado**, de propósito: outros consumidores podem continuar batendo direto na
  origem antiga.
