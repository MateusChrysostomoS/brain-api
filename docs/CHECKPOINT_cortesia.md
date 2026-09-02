# CHECKPOINT — cupom de cortesia (acesso sem Stripe)

> Feature de "family and friends": quem resgata um cupom válido no /cadastro tem a clínica
> ativada na hora — sem cartão, sem assinatura, sem cobrança. **NO AR desde 2026-08-25**
> (`d130063`), com o caminho já exercitado em produção. Encerrada em **2026-09-02** com as
> duas telas que ainda afirmavam um pagamento inexistente.
>
> Atravessa TRÊS repos: brain-api (o resgate), brain-frontend (o campo + a tela de retorno)
> e PreCheck (a tela onde o médico cai depois do SSO).

## 1. Por que não é um cupom de 100% off no Stripe

A alternativa óbvia foi descartada de propósito, e vale registrar para ninguém "simplificar"
isso depois:

- o Checkout do signup é `mode=setup`, que **captura cartão antes de qualquer desconto** —
  um cupom de 100% off ainda pediria o cartão do amigo;
- sobraria uma **assinatura viva no Stripe**, que volta a cobrar sozinha quando o desconto
  vence. Ninguém lembra de cancelar em N meses.

Com a cortesia não existe **nada** no Stripe para esse tenant: nada para expirar, cobrar ou
cancelar. O preço disso é que a clínica de cortesia não tem `stripe_customer_id` nem
`stripe_subscription_id` — daí `signup_intents.courtesy_coupon_code`, que é o que distingue
"ativado de graça" de "falha de cobrança" numa auditoria.

## 2. O caminho, e onde ele se encaixa no pago

O caminho pago é: **registra** (tenant + owner + entitlement inerte) → **Checkout** →
**webhook** → `signup.provision_tenant_from_intent` **ATIVA** o entitlement.

A cortesia entra exatamente no último passo, pulando os dois do meio. **Não é uma segunda
implementação de ativação** — é a mesma função, chamada sem ids do Stripe, no caso que ela
já documentava como "no-Stripe-ids" e resolve para `status="active"`.

| Peça | Onde |
|---|---|
| Modelo do cupom | `models/courtesy_coupon.py` (`courtesy_coupons`) |
| Auditoria no intent | `models/signup_intent.py::courtesy_coupon_code` |
| Regra de resgate | `services/courtesy.py::redeem` |
| Rota pública | `POST /public/courtesy-redemptions` (`api/public_signup.py`) |
| Migration | `0016_courtesy_coupons` |
| Cadastro de cupom | `scripts/create_courtesy_coupon.py` |
| Campo "Tenho um cupom" | brain-frontend `cadastro/_components/SummaryStep.tsx` |
| Tela de retorno | brain-frontend `/checkout/sucesso?courtesy=1` |
| Tela do outro lado do SSO | PreCheck `frontend/app/(SignOut)/sso/page.tsx` |

Depois do resgate a rota dispara **a mesma ponte do PreCheck** que o webhook dispara
(`onboarding_sync.ensure_precheck_provisioned`), pós-commit e gated no entitlement. Sem ela
`precheck_account_links` fica vazio e o handoff `POST /sso/precheck/token` responde 409 — o
médico entraria em nada.

## 3. Os portões (isto dá acesso pago de graça)

- `is_active` / `expires_at` / `max_uses`, com `uses` incrementado **sob lock de linha**
  (`with_for_update`): sem ele, dois cliques simultâneos no último uso passariam os dois.
- O **plano vem do CUPOM**, não do que o visitante selecionou — senão bastaria escolher o
  plano caro antes de resgatar um cupom do básico. Os add-ons dele são descartados junto.
- As **quatro recusas** (inexistente, desativado, expirado, esgotado) devolvem a **mesma**
  mensagem `coupon_invalid`. Distingui-las mapearia para um atacante quais códigos existem.
  Só o log (`courtesy_redeem_refused`) separa os motivos.
- Ativação que falha faz `rollback` e **não gasta o uso** — no webhook um sucesso silencioso
  é aceitável (o Stripe precisa do ack), aqui há alguém esperando na tela.

## 4. Estado em produção (medido em 2026-09-02)

`alembic_version` = `0016_courtesy_coupons` — migration aplicada.

| code | plano | ativo | usos | limite | expira |
|---|---|---|---|---|---|
| `CORTESIA100` | `precheck_basic` | sim | **2** | 26 | sem prazo |

⚠️ A `note` do cupom diz "1 uso gasto no smoke de 25/08" e o contador está em **2** — ou
houve um segundo resgate real, ou um segundo smoke que ninguém anotou. Restam **24**.

⚠️ O cupom **não tem prazo**. `--expires-in-days` existe e não foi usado: um código sem
validade vazado num grupo de WhatsApp vira produto grátis até alguém notar. Desligar é
`scripts/create_courtesy_coupon.py --code CORTESIA100 --deactivate`.

## 5. O que entrou em 2026-09-02 (o fechamento) — NO AR

Deployado e conferido em produção no mesmo dia. Provas: `_precheck_identity` no container
`srsjjutcs531b5r6wimclu4qz` devolve `precheck clinica sao jose` (frase em palavras, sem
sufixo); o chunk do `/sso` em `precheck.com.br` traz "Sua clínica está pronta" e **zero**
ocorrências de "Pagamento confirmado"; `brainai.com.br/checkout/sucesso/?courtesy=1` abre
em "Estamos preparando sua conta…" e resolve para "Sua conta já está pronta / Entre com o
e-mail usado no cadastro".


O backend estava pronto desde 25/08; o que faltava era a **verdade nas telas**. O token do
SSO não diz se a clínica foi ativada por pagamento ou por cupom — e as duas telas do fim do
funil afirmavam a cobrança para os dois grupos.

| Onde | Antes | Agora |
|---|---|---|
| PreCheck `/sso` (`6bcb0f8`) | "Pagamento confirmado!" | "Sua clínica está pronta." |
| brain-frontend `/checkout/sucesso` (`96a08af`) | "Pagamento confirmado!" em 4 estados | `tituloOk`: "Tudo pronto!" na cortesia |

Os quatro estados alcançáveis por quem resgatou: `polling` (o estado **inicial** dos dois
caminhos — `view` nasce "polling" também na cortesia e só sai depois do `ensureSession()`),
`ready-precheck`, `ready-precheck-pending` e `ready-already-claimed`. Os corpos que diziam
"o e-mail usado na compra" viraram "usado no cadastro" — verdadeiro nos dois, já que a conta
é criada no primeiro card do wizard, antes de qualquer compra.

Junto foi a **frase-gatilho em palavras** (`dff597d` aqui, `6bcb0f8` no PreCheck): vale para
os dois caminhos de ativação, porque ambos chamam `ensure_precheck_provisioned`. Ver §6.

## 6. Frase-gatilho: por que ela mudou junto

`_precheck_identity` gerava slug **e** frase com o mesmo formato — `precheck
clinica-do-coracao 6bac6f6c`. O slug pode: é PK natural e ninguém o lê. A frase não: é o que
o paciente vê no link `wa.me`, no QR impresso na recepção e o que digita à mão quando não
clica em nenhum dos dois.

Pior que a estética: o PreCheck resolve colisão de gatilho removendo as **palavras** que
batem com clínicas existentes (`_sem_termos_conflitantes` separa por espaço). Com o slug
hifenizado o nome inteiro era **uma palavra só** — bastava um termo ofensor no meio (a
clínica "Quero apenas testar" registra `testar,teste`) para a frase toda ir para o lixo e a
clínica cair no sufixo hexadecimal.

Agora a frase sai limpa e a unicidade é do PreCheck, que já sabia fazer isso desde `88ed6e5`:
`_resolve_trigger_phrase` detecta a colisão, tenta variações que ainda carregam o nome e só
então acrescenta o sufixo. O provisionamento devolve a frase **efetiva**, que é a que este
bridge persiste.

## 7. Pendências

- **Sem admin para cupom.** Criar/desligar é só pelo script, via SSH no container. Aceitável
  enquanto é family-and-friends; vira problema no dia em que houver cupom de campanha.
- **Nunca houve smoke ao vivo do fluxo inteiro depois de 25/08.** O contador em 2 sugere que
  alguém passou, mas não há registro do que foi verificado.
- **`CORTESIA100` sem prazo** — ver §4.
