# Guia: Obtenção e Configuração das Credenciais Meta para Embedded Signup

Este documento descreve como obter e configurar as credenciais do Meta Embedded Signup necessárias para que o onboarding automático de WhatsApp na Brain funcione. O guia é dividido em duas partes: obtenção das credenciais no painel da Meta (Parte A) e aplicação no EasyPanel (Parte B).

## Parte A — Obtenção das Credenciais no Painel da Meta

### Pré-requisito: Tech Provider Program da Meta

**ANTES de prosseguir, você deve estar aprovado no Tech Provider Program da Meta para WhatsApp.**

Sem essa aprovação, as credenciais obtidas não funcionarão para onboarding de clientes. O Tech Provider Program exige:

1. **Business Verification** — Meta verifica seus dados da empresa
2. **App Review** — Seu app Meta recebe análise de segurança e conformidade com [vídeos demonstrando envio de mensagens](https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/get-started-for-tech-providers)
3. **Advanced Access** — Aprovação para as permissões `whatsapp_business_messaging` e `whatsapp_business_management`

Após aprovação, você terá um limite inicial de até 10 novos clientes onboarded a cada 7 dias (limite ampliado para 200 após completar verificações).

**Para verificar seu status ou começar o processo:**
- Acesse [Meta Tech Provider Program — Get Started](https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/get-started-for-tech-providers)
- Ou entre em contato com a equipe de Meta Partners

### 1. App ID e App Secret

Após a aprovação do Tech Provider Program, siga estes passos:

1. Entre em [developers.facebook.com](https://developers.facebook.com) com sua conta Meta
2. Clique em **"Meus Apps"** (My Apps) no canto superior direito
3. Selecione seu **app Meta** na lista (deve ser configurado para WhatsApp Business)
4. No menu esquerdo, escolha **Configurações do App** → **Básico** (App Settings → Basic)
5. Nesta página, você encontrará:
   - **App ID**: copie o valor
   - **App Secret**: copie o valor (cuidado: este é um segredo — nunca compartilhe)

**Observação importante:** A interface do painel da Meta pode sofrer atualizações. Se o caminho acima não corresponder exatamente ao que você vê, procure por "App Settings" ou "Basic" no menu lateral, ou consulte a [documentação oficial de credenciais do Facebook Graph API](https://developers.facebook.com/docs/facebook-login/guides/access-tokens/).

### 2. Configuration ID do Embedded Signup (caminho v4 — ver seção "Versão do Embedded Signup")

**IMPORTANTE (atualizado 2026-08-01):** crie a configuração pelo caminho abaixo — é o que
garante que o fluxo sai como **v4**, a versão ativa (v2 depreca em **2026-10-15**). Veja a
seção "Versão do Embedded Signup" mais abaixo neste documento para o raciocínio completo e
as fontes oficiais.

1. No mesmo app Meta em [developers.facebook.com](https://developers.facebook.com), vá em
   **App Dashboard → Facebook Login for Business → Configurations**
2. Clique em criar uma nova configuração
3. Selecione **Embedded Signup** como "login variation"
4. Selecione os produtos desejados (tipicamente WhatsApp Cloud API) — **selecionar os
   produtos aqui é o que ativa automaticamente a v4**, segundo a doc oficial (ver seção
   "Versão do Embedded Signup")
5. Copie o **Configuration ID** gerado

**Observação sobre a UI:** O painel da Meta sofre atualizações frequentes. Os rótulos exatos
("Embedded Signup", "Configuration ID", "Facebook Login for Business", etc.) podem variar.
Se não encontrar estas opções exatamente, procure na [documentação oficial de Embedded
Signup — Versions](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/versions),
[Version 4](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/version-4)
ou [Implementation](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation).

**Evite** o caminho antigo ("Produtos → WhatsApp → Embedded Signup" direto, sem passar por
Facebook Login for Business) — esse é o caminho de v2/v3, que a Meta está descontinuando.

### 3. Webhook fields e verify token (necessário para o fluxo de Coexistence)

**Adicionado nesta rodada** ("WhatsApp Coexistence onboarding") — faltava no guia até
aqui. O fluxo de Coexistence depende de a app estar assinada nos webhook fields corretos
na WABA do cliente (isso é o que `services/meta_graph.py::subscribe_app_to_waba` faz
automaticamente via `POST /{waba_id}/subscribed_apps` — ver `CONTRACTS.md` §16.2), mas a
CONFIGURAÇÃO de quais fields existem/são assinados pela sua APP (não pela WABA do cliente)
é feita uma vez, manualmente, no painel:

1. No app Meta em [developers.facebook.com](https://developers.facebook.com), vá em
   **App Dashboard → WhatsApp → Configuration**
2. Na seção de **Webhooks**, assine (subscribe) os seguintes campos, além dos que a Brain
   já usa para o fluxo padrão:
   - `messages` — mensagens recebidas (já necessário no fluxo padrão)
   - `smb_message_echoes` — ecos das mensagens que o CLIENTE envia pelo próprio app
     WhatsApp Business (necessário para a secretarIA saber que o dono respondeu
     manualmente — sinal de "modo humano")
   - `history` — sincronização histórica na transição de Coexistence
   - `smb_app_state_sync` — sinal de mudança de estado do app WhatsApp Business do
     cliente (usado por secretarIA para resolver `mode_resolved`; ver
     `MODE_RESOLVE_FALLBACK_HOURS` em `CONTRACTS.md` §7)
3. Configure o **Verify Token** do webhook como o valor de `META_VERIFY_TOKEN` do
   secretarIA (é secretarIA, não brain-api, quem recebe o webhook Meta diretamente — ver
   `docs/CHECKPOINT_coexistence.md` para o ponteiro de responsabilidade). Confirme com o
   time do secretarIA qual é o valor atual antes de configurar.

**Observação:** brain-api nunca recebe o webhook Meta diretamente — apenas chama
`subscribed_apps` para GARANTIR que a app está assinada na WABA de cada cliente
individualmente, o que é diferente desta configuração (que é da APP como um todo, feita
uma vez no painel).

---

## Parte B — Aplicação no EasyPanel

### 1. Configuração do brain-api

O serviço **brain-api** é responsável por fazer a troca de código por token (OAuth) com a Meta. Você precisa configurar três variáveis de ambiente:

#### No EasyPanel:
1. Abra a aba de seu projeto
2. Localize o serviço **brain-api**
3. Acesse **Variáveis de Ambiente** (Environment)
4. Localize (ou crie, se ainda não existir) as seguintes variáveis:

| Variável | Valor | Notas |
|---|---|---|
| `META_APP_ID` | `<SEU_APP_ID>` | Copie do painel da Meta (Configurações Básicas). |
| `META_APP_SECRET` | `<SEU_APP_SECRET>` | Copie do painel da Meta (Configurações Básicas). **SEGREDO** — nunca exponha em frontend ou logs. |
| `META_ES_CONFIG_ID` | `<SEU_CONFIGURATION_ID>` | Copie da configuração de Embedded Signup no painel da Meta. |
| `META_GRAPH_BASE_URL` | `https://graph.facebook.com/v23.0` | Já configurado. Mantenha este valor (é a versão da API Meta que brain-api usa). |
| `META_ES_COEXISTENCE_FEATURE_TYPE` | `whatsapp_business_app_onboarding` (default no código) | O valor de `extras.featureType` que ativa o fluxo de Coexistence no Embedded Signup (onboarding de clientes que já usam o app WhatsApp Business, em vez de número novo). Echoado read-only via `GET /doctor/onboarding`'s `embedded_signup.coexistence_feature_type`, para o frontend decidir se oferece a opção "já uso este número no WhatsApp Business". Vazio = opção não oferecida no portal. Não é segredo. |

**Após preencher:**
- Clique em **Salvar** ou **Deploy**
- Aguarde o restart/redeploy do serviço brain-api

**Validação rápida:** Com `META_APP_ID` e `META_ES_CONFIG_ID` setados, um doctor logado pode acessar `GET /doctor/onboarding` e receberá um objeto `embedded_signup` com `configured: true` (veja a Validação, mais abaixo).

### brain-frontend: Nenhuma Configuração Necessária

**O brain-frontend NÃO requer variáveis de ambiente nem rebuild para usar Embedded Signup.**

O app ID e Configuration ID são entregues em **tempo de execução** (runtime) pela resposta de `GET /doctor/onboarding` do brain-api — especificamente no bloco `embedded_signup`. Quando um doctor carrega `/app/onboarding`, o frontend obtém esses valores diretamente do backend, sem necessidade de variáveis ambientais baked into the build. Isso permite que você mude a configuração da Meta no EasyPanel e veja o efeito no frontend imediatamente, sem rebuild.

**Referência:** [brain-frontend/docs/VERIFICATION_onboarding.md (linhas 19–26)](file://C:\TECH\BRAIN\brain-frontend\docs\VERIFICATION_onboarding.md) documenta explicitamente que `app_id`/`config_id` são obtidos em runtime de `GET /doctor/onboarding`'s bloco `embedded_signup`, preenchido pelo brain-api a partir de suas settings (`META_APP_ID`/`META_ES_CONFIG_ID`).

---

## Validação

### Critério 1: backend respondendo `configured=true`

Após configurar `META_APP_ID` e `META_ES_CONFIG_ID` no brain-api e reiniciar o serviço:

1. Faça login como um doctor (tenha uma clínica com onboarding em progresso)
2. Faça uma requisição GET para `/doctor/onboarding`
3. Procure no response pelo objeto `embedded_signup`
4. Confirme que tem `"configured": true`:

```json
{
  "onboarding_state": "aguecimento",
  "blocker_reason": null,
  "config_status": "...",
  "embedded_signup": {
    "configured": true,
    "app_id": "<SEU_APP_ID>",
    "config_id": "<SEU_CONFIGURATION_ID>"
  }
  ...
}
```

Se `configured` for `false`, revise:
- `META_APP_ID` está preenchido e não vazio?
- `META_ES_CONFIG_ID` está preenchido e não vazio?
- O serviço brain-api foi reiniciado após as mudanças?

### Critério 2: frontend exibindo o botão ativo

Uma vez que o backend responda com `embedded_signup.configured: true`, o botão de Embedded Signup no frontend ativa automaticamente no próximo carregamento da página:

1. Faça login no portal (`brain-frontend`) como um doctor
2. Navegue para `/app/onboarding`
3. Procure pela seção "Tentar ativar agora"
4. O botão **deve estar habilitado** (clicável) e **não deve mostrar** a mensagem "Ativação assistida ainda não configurada"
5. Se clicar no botão, um popup da Meta deve abrir (o SDK da Meta foi carregado corretamente com as credenciais do backend)

Se o botão continuar desabilitado:
- Limpe o cache do navegador (Ctrl+Shift+Delete ou Cmd+Shift+Delete)
- Recarregue a página
- Revise se `META_APP_ID` e `META_ES_CONFIG_ID` foram setados corretamente no brain-api (sem espaços extras)
- Confirme que o brain-api foi reiniciado após as mudanças

---

## Versão do Embedded Signup (v2 vs v4)

**Dúvida original (levantada 2026-08-01):** ao revisar links da doc da Meta, apareceu o
aviso de que o Embedded Signup **"v2" será depreciado em 2026-10-15** e que **"v4" é a
versão ativa**. Não estava confirmado se o código do brain-frontend
(`app/(site)/app/onboarding/lib/meta-embedded-signup.ts`) já correspondia à v4, ou se o
número `v23.0` (`SDK_VERSION`/`META_GRAPH_BASE_URL`) tinha alguma relação com essa versão.
Esta seção fecha a dúvida com fonte oficial (fechado 2026-08-01).

### Conclusão: são três eixos independentes

1. **Versão do Graph API / JS SDK** — `SDK_VERSION = "v23.0"` no frontend e
   `META_GRAPH_BASE_URL=https://graph.facebook.com/v23.0` no backend. Isso versiona a API
   do Graph/JS SDK, **não** o fluxo de Embedded Signup.
2. **`sessionInfoVersion`** — campo do `extras` do `FB.login`, era da v2 (ver abaixo). Não é
   a versão do fluxo.
3. **Versão do fluxo de Embedded Signup em si (v2/v3/v4)** — é o que estava em dúvida.
   **Determinada exclusivamente por QUAL `config_id` é usado no `FB.login()`** — ou seja,
   qual **Facebook Login for Business Configuration** foi criada no App Dashboard da Meta.
   Fonte oficial (verbatim):

   > "To upgrade to the v4 experience, you need to create a new [Facebook Login for
   > Business Configuration]...and select your desired products. Selecting the products
   > will automatically set you to v4."
   > — https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/version-4

   Passos oficiais (mesma fonte): App Dashboard → **Facebook Login for Business** →
   **Configurations** → criar nova → selecionar **Embedded Signup** como "login variation"
   → selecionar os produtos desejados → copiar o **Configuration ID** para o SDK. Passo
   aplicado na Parte A, seção 2, acima.

Linha do tempo oficial
(https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/versions):

| Versão | Lançamento | Disponível até |
|---|---|---|
| v2 | Janeiro de 2023 | **depreciação em 2026-10-15** |
| v2-public-preview | 14 ago 2025 | Outubro de 2026 |
| v3 | 29 mai 2025 | Outubro de 2026 |
| v3-public-preview | 14 ago 2025 | Outubro de 2026 |
| **v4** | **8 out 2025** | versão ativa/atual |
| v4-public-preview | 12 mai 2026 | TBD |

> "The current Embedded Signup Version is v4." / "Embedded signup v2 will be deprecated on
> October 15, 2026." — mesma fonte acima; aviso repetido em
> https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/overview

### O que `sessionInfoVersion: "3"` de fato é

Campo **legado, da era v2**, do objeto `extras`: pedia que o evento de conclusão
(`window.postMessage` tipo `WA_EMBEDDED_SIGNUP`, evento `FINISH`) viesse com
`phone_number_id`/`waba_id` já embutidos no payload. Fontes oficiais:

> v2: "Partners must add `sessionInfoVersion` for callbacks."
> v4: "Session info logging sent for all flows" — o payload enriquecido que a v2 exigia
> pedir via `sessionInfoVersion: "3"` passa a ser **sempre** enviado por padrão; o exemplo
> de código oficial da v4 mostra `extras: {}` — **"the extras object is purposely empty for
> v4"**.
> — https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/versions
> — https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation

**Não há um valor mais novo de `sessionInfoVersion`** (não é "sessionInfoVersion: 4"): a doc
oficial da v4 remove o campo em vez de incrementá-lo — foi substituído pelo comportamento
"sempre ligado" da v4. (Uma fonte não-oficial de terceiros —
https://www.unifyport.ai/blog/whatsapp-embedded-signup-v4-coexistence-migration/ — observa
que o payload de sessão pode carregar internamente um campo `"version": 3` referente ao
ESQUEMA do payload, um terceiro eixo ainda diferente; não encontrei confirmação disso em
página oficial da Meta, então trato como não-verificado.)

### O código estava em v2 ou v4?

Como estava, o `extras` do `FB.login` (`{ setup: {}, featureType: "", sessionInfoVersion:
"3" }`) seguia exatamente o formato documentado para **v2** — `sessionInfoVersion` é
característica de v2, ausente do exemplo oficial de v4. Como nenhuma credencial está
configurada em produção ainda (Tech Provider Program pendente de aprovação — Parte A acima
— `META_APP_ID`/`META_ES_CONFIG_ID` vazios), não havia como confirmar pelo `config_id` real
qual fluxo rodaria de fato; a análise foi 100% por código + doc oficial, sem teste ao vivo.

**Ação tomada (2026-08-01):**
1. `meta-embedded-signup.ts` — `extras` do `FB.login` simplificado para `{ setup: {} }`
   (formato oficial de v4), removendo `featureType`/`sessionInfoVersion` (campos de v2);
   comentários no código apontam para esta seção. Validado com `tsc --noEmit` e
   `next build` — ambos limpos, sem erros.
2. Este guia (Parte A, seção 2, acima) — instruções de criação do Configuration ID
   corrigidas para o caminho oficial de v4 (Facebook Login for Business → Configurations →
   "Embedded Signup" → selecionar produtos), em vez do caminho antigo
   ("Produtos → WhatsApp → Embedded Signup" direto), que é o caminho de v2/v3.
3. **Nenhuma mudança em `brain-api`** foi necessária: `services/meta_graph.py`'s
   `exchange_code_for_token` usa `GET {META_GRAPH_BASE_URL}/oauth/access_token`, o endpoint
   padrão de troca de código OAuth — versionado pelo Graph API (`META_GRAPH_BASE_URL`), não
   pelo eixo v2/v4 do Embedded Signup. Os campos que `POST /doctor/onboarding/attempts`
   consome do frontend (`code`, `phone_number_id`, `waba_id`) continuam presentes no
   payload de conclusão da v4 — a doc oficial de Implementation descreve o payload de
   sucesso da v4 como `phone_number_id`, `waba_id`, `business_id`, mais IDs de ativos
   opcionais quando outros produtos são incluídos: um superset compatível, não um formato
   diferente.

### Pendência (fora do escopo de código)

A versão efetivamente usada em produção só fica 100% garantida quando alguém, já aprovado
no Tech Provider Program, criar o **Configuration ID** pelo caminho v4 descrito na Parte A,
seção 2 (uma ação no painel da Meta, não uma mudança de código/repo). **Isto não pôde ser
testado ao vivo nesta rodada** — Tech Provider Program ainda não aprovado, sem credenciais
configuradas (ver Parte A). A validação aqui é código + doc oficial, não um teste
ponta-a-ponta.

### Fontes oficiais consultadas (2026-08-01)

- https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/versions
- https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/version-4
- https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation
- https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/overview
- https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users/
  (fluxo de coexistência/"whatsapp_business_app_onboarding" — **a partir desta rodada
  (rodada "WhatsApp Coexistence onboarding") a Brain OFERECE este fluxo como opção**,
  junto do fluxo padrão de número novo; ver a seção "Coexistence — Task 0: grafia do
  feature type" mais abaixo para o detalhe completo)

---

## Coexistence — Task 0: grafia do feature type (pesquisa fechada, rodada "WhatsApp Coexistence onboarding")

**Dúvida:** qual é a grafia exata do parâmetro que ativa o fluxo de Coexistence no
Embedded Signup — `featureType` (camelCase) ou `feature_type` (snake_case) — e em qual
objeto ele vive (`extras` direto ou aninhado)? Fechada com fontes oficiais, sem teste ao
vivo (nenhuma credencial configurada em produção ainda — ver "Pendência" acima).

**Citações (verbatim, já pesquisadas nesta rodada):**

- Página v4 (`.../embedded-signup/version-4`): em prosa, **"Onboarding WhatsApp Business
  app users continues to be supported through the `feature_type` parameter"** — mas essa
  página não mostra nenhum snippet JS com o parâmetro.
- Página Implementation (`.../embedded-signup/implementation`): o snippet de código
  mostra `extras: { setup: {} }`, sem feature type nenhum. A mesma página lista os
  eventos de conclusão possíveis: `FINISH`, `FINISH_ONLY_WABA`,
  `FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING`, `FINISH_OBO_MIGRATION`,
  `FINISH_GRANT_ONLY_API_ACCESS` — com payload `phone_number_id`, `waba_id`,
  `business_id`.
- Doc legada de Coexistence
  (`.../embedded-signup/custom-flows/onboarding-business-app-users/`): a chave JS
  correta é **`extras.featureType`** (camelCase), com valor
  **`whatsapp_business_app_onboarding`** — o valor antigo `coexistence` **deixou de ser
  válido**. O evento de conclusão correspondente é
  `FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING`. Esta mesma doc exige assinar os webhook
  fields `history`, `smb_app_state_sync`, `smb_message_echoes` (ver Parte A, seção 3,
  acima).

**Conclusão adotada:** a chave JS é `featureType` (camelCase) DENTRO de `extras` —
`extras.featureType = "whatsapp_business_app_onboarding"`. A prosa da página v4 usa
`feature_type` (snake_case) para se REFERIR ao mesmo parâmetro em texto corrido, não como
grafia literal de código — não há contradição real, apenas duas convenções de nomenclatura
(uma de prosa, uma de código) apontando para o mesmo campo. `whatsapp_business_app_onboarding`
substitui o valor legado `coexistence`, que não é mais aceito.

O frontend loga em debug (fora de produção) todo evento `WA_EMBEDDED_SIGNUP` recebido via
`window.postMessage`, o que permite confirmar ao vivo — quando houver credenciais reais —
qual evento de conclusão (`FINISH` vs. `FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING`) o SDK da
Meta efetivamente dispara para cada fluxo.

**No brain-api**, esta pesquisa se reflete apenas na nova setting
`META_ES_COEXISTENCE_FEATURE_TYPE` (default `"whatsapp_business_app_onboarding"`, Parte B
acima) — o backend não faz nenhuma chamada JS, apenas ecoa esse valor read-only via
`GET /doctor/onboarding`. A implementação do `FB.login()`/`extras.featureType` em si é
responsabilidade do brain-frontend (fora deste repo).

---

## Resumo das Etapas

| Etapa | O que fazer | Responsável |
|---|---|---|
| 1. Tech Provider Program | Verificar/solicitar aprovação no programa da Meta | Dono do produto / operações |
| 2. Obter credenciais | Copiar App ID, App Secret, Configuration ID do painel da Meta | Dono do produto |
| 3. EasyPanel — brain-api | Settar `META_APP_ID`, `META_APP_SECRET`, `META_ES_CONFIG_ID` e reiniciar o serviço | DevOps / EasyPanel admin |
| 4. Validação | Confirmar `embedded_signup.configured: true` no backend e botão ativo no frontend | QA / tester |

---

## Referências

- [Meta Embedded Signup — Overview](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/overview)
- [Meta Embedded Signup — Implementation](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation)
- [Meta Embedded Signup — Versions](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/versions)
- [Meta Embedded Signup — Version 4](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/version-4)
- [Meta Tech Provider Program — Get Started](https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/get-started-for-tech-providers)
- [Facebook Graph API — Access Tokens](https://developers.facebook.com/docs/facebook-login/guides/access-tokens/)
- Code: `brain-api/src/brain_api/config.py` (linhas ~225), `brain-api/src/brain_api/api/onboarding.py` (linhas ~94–100), `brain-api/src/brain_api/services/meta_graph.py`
- Code (frontend): `brain-frontend/app/(site)/app/onboarding/lib/meta-embedded-signup.ts`
- Contract: `brain-api/CONTRACTS.md` (§16.2, linhas ~592–594)

---

**Data da escrita:** 2026-07-31  
**Data da última atualização:** 2026-08-09 — rodada "WhatsApp Coexistence onboarding"
(brain-api): (1) corrigido "Fontes oficiais consultadas" — a Brain agora OFERECE o fluxo
de coexistência como opção; (2) `META_ES_COEXISTENCE_FEATURE_TYPE` documentada na tabela
da Parte B; (3) Parte A ganhou a seção 3 (webhook fields `messages`/
`smb_message_echoes`/`history`/`smb_app_state_sync` + verify token do secretarIA); (4)
seção "Coexistence — Task 0" adicionada com as citações exatas que fecham a grafia do
feature type (`extras.featureType`, camelCase). Ver `docs/CHECKPOINT_coexistence.md` para
o detalhe completo desta rodada.  
**Versão:** 1.3  
**Status:** Guia completo — pronto para aplicação em produção e dev/staging. Migração de
código para v4 feita e validada (tsc/build); config_id real ainda não existe (Tech Provider
Program pendente) — ver seção "Versão do Embedded Signup" para a pendência. Fluxo de
Coexistence agora documentado end-to-end (Parte A §2-3, Task 0) mas ainda sem validação ao
vivo com número real elegível.
