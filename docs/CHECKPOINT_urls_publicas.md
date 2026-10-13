# TASK-055 — URLs públicas por serviço

Implementado e validado em 2026-10-12. Commit e push para `origin/main` autorizados pelo usuário nesta continuação da TASK-055. Sem deploy manual, SQL remoto ou alteração de variáveis em produção. Revisão baseada no código local; não foi feita consulta às configurações do Easypanel. Registro dos commits e confirmação remota em `../../tasks/TASK-055/TASK.md`.

## Configuração mínima

| Serviço | Variável | Conteúdo |
|---|---|---|
| brain-api | `FRONTEND_BASE_URL` | URL base pública do brain-frontend, sem rota |
| brain-api | `BRAIN_MESSAGE_PUBLIC_URL` | URL base pública do Brain-Message, sem rota |
| secretaria_api | `BRAIN_MESSAGE_PUBLIC_URL` | A mesma base pública do Brain-Message |
| secretaria-worker | `BRAIN_MESSAGE_PUBLIC_URL` | A mesma base pública do Brain-Message |

Para o endereço informado pelo usuário:

```dotenv
BRAIN_MESSAGE_PUBLIC_URL=https://precheckv2-brain-message-frontend.cpux9k.easypanel.host
```

O envio do convite de equipe é iniciado no **brain-api**. A secretarIA entrega o e-mail com o link recebido; suas próprias notificações e lembretes também precisam da base pública. Não configurar essa variável no frontend para construir o convite. Os frontends usam suas variáveis de build já existentes.

## Rotas no código

| Base | Destino derivado |
|---|---|
| `BRAIN_MESSAGE_PUBLIC_URL` | `/login` para primeiro acesso da equipe |
| `BRAIN_MESSAGE_PUBLIC_URL` | `/clinicas/?convite=...` para pacientes |
| `BRAIN_MESSAGE_PUBLIC_URL` | `/agenda` para avisos aos profissionais |
| `BRAIN_MESSAGE_PUBLIC_URL` | `/contexto?status=...` após conectar Google Calendar |
| `FRONTEND_BASE_URL` | `/app?checkout=success` ou `/app?checkout=cancelled` para compras comuns/avulsas |
| `FRONTEND_BASE_URL` | `/checkout/sucesso/?session_id={CHECKOUT_SESSION_ID}` para cadastro; com `origem=console` para retorno ao console |
| `FRONTEND_BASE_URL` | `/checkout/cancelado/` para cancelamento do cadastro |
| `FRONTEND_BASE_URL` | `/app` ao sair do portal de cobrança |

`Settings` mantém propriedades calculadas com os nomes dos retornos Stripe, da agenda e do retorno OAuth para seus consumidores internos. Essas propriedades **não são variáveis de ambiente** e não leem os valores antigos. Compras existentes/avulsas não consultam onboarding de cadastro. No checkout com `return_to=console`, a página pública usa o ramo de retorno ao console. `success_url_for` continua acrescentando `origem=console` e `produto=precheck` quando aplicável, sem codificar o template Stripe.

`team.staff_login_url` valida a origem antes de emitir convites: HTTPS fora de dev/test, sem credenciais, caminho, query ou fragmento. A configuração antiga mostrada com `/login` era rejeitada porque a validação já exigia a raiz. Agora a base recebe `/login` no código. Barras finais e espaços externos são normalizados.

Base vazia: convite de equipe recusado antes da criação; convite de paciente sem link; notificações sem linha de agenda; retorno Google em JSON. Códigos de convite e demais contratos HTTP permanecem iguais.

## Migração para uma publicação futura

1. Publicar Brain-Message-Frontend com a nova página `/login`, que reutiliza a entrada existente. A raiz continua funcionando. Não há nova lógica de autenticação.
2. Configurar as bases da tabela e publicar brain-api, secretaria_api e secretaria-worker com este código. Garantir que `FRONTEND_BASE_URL` seja o endereço público correto antes de usar pagamentos; os retornos não dependem mais das URLs Stripe antigas.
3. Na configuração das versões novas, retirar os nomes substituídos abaixo. Não retirar variáveis de serviços que ainda executam o código antigo. Para rollback, restaurar também a configuração correspondente à versão anterior.

| Serviço | Nomes substituídos |
|---|---|
| brain-api | `BRAIN_MESSAGE_STAFF_LOGIN_URL`, `BRAIN_MESSAGE_PORTAL_URL`, `STRIPE_CHECKOUT_SUCCESS_URL`, `STRIPE_CHECKOUT_CANCEL_URL`, `STRIPE_SIGNUP_CHECKOUT_SUCCESS_URL`, `STRIPE_SIGNUP_CHECKOUT_CANCEL_URL`, `STRIPE_PORTAL_RETURN_URL` |
| secretarIA API/worker | `BRAIN_MESSAGE_PORTAL_URL`, `DOCTOR_AGENDA_URL`, `PORTAL_POST_OAUTH_REDIRECT` |

Os modelos dos dois backends passam de onze configurações desses links para três entradas: duas bases no brain-api e uma na secretarIA. Redução de oito entradas, sem contar a duplicação da configuração entre API e worker.

## Revisão dos outros serviços

PreCheck já usa `FRONTEND_BASE_URL`, `BRAIN_API_URL` e sua única `BRAIN_MESSAGE_PORTAL_URL` para destinos distintos; o link do portal já acrescenta a rota no código. O nome existente do PreCheck permanece nesta revisão, pois não duplica outra configuração de URL ali. brain-frontend, secretarIA-frontend e Brain-Message-Frontend já separam bases de APIs e navegação entre produtos. Essas bases foram mantidas. Prefixos de proxy do navegador preservam cookies na mesma origem; não podem ser trocados pela URL de outro backend indiscriminadamente.

`GOOGLE_OAUTH_REDIRECT_URI` na secretarIA permanece: é o callback da API cadastrado no Google, um destino diferente do retorno do navegador ao portal. Não há outra base pública própria da secretarIA com a qual eliminar essa entrada sem introduzir uma nova variável. URLs de infraestrutura, CORS, credenciais e provedores independentes não foram consolidadas.

## Validação

- brain-api: 179 testes de URLs, equipe, convites, configuração, cadastro e cobrança; mais 68 de retorno ao console e recarga/upgrade PreCheck. Todos passaram, sem chamadas externas reais.
- Após corrigir a diferença entre retorno comum/cadastro/console: 130 testes de URLs, retorno ao console, compras PreCheck e cadastro repetidos; todos passaram na versão final.
- secretarIA: 109 testes de URLs, notificações, avisos e lembretes; mais 20 de URLs/OAuth (quatro repetidos). Todos passaram.
- Frontend: `npm run build` passou, incluindo tipos e export estático de `/login/index.html`.
- Ruff nos arquivos alterados passou, exceto `F811` em `test_team.py`, confirmado idêntico no HEAD anterior. Avisos de depreciação Starlette e rewrites com static export registrados; não são erros novos.
- Para testar a secretarIA foi necessário `PYTHONPATH` explícito para este checkout: seu ambiente editable apontava para um worktree antigo. Não foi alterada a instalação local.

Revisão independente identificou e corrigiu o retorno incorreto de compras comuns à página exclusiva de cadastro. Os testes distinguem esses retornos e o ramo do console; correção reavaliada sem findings.

Estado operacional e revisão em `../../tasks/TASK-055/`.
