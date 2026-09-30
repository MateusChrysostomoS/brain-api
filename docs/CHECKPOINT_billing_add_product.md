# CHECKPOINT — adicionar produto à assinatura (TASK C / TASK-015)

Spec: `Brain-Message-Frontend/docs/superpowers/specs/2026-09-29-task-c-adicionar-produto-na-assinatura-design.md`. Plano: `Brain-Message-Frontend/docs/superpowers/plans/2026-09-29-task-c-adicionar-produto-na-assinatura.md`.

## 1. Estado

| Camada | Local | Commitado | Deployado |
|---|---|---|---|
| brain-api e migração `0026_entitlement_product_state` | implementados; migração provada em SQLite; suíte: 1224 passaram, 3 SKIP, 0 falhas | não | não; migração não aplicada em produção |
| brain-frontend | TypeScript, 293 testes e build estático verdes | não | não |
| Brain-Message-Frontend | TypeScript, 1405 testes e build estático verdes | não | não |
| Stripe real e Postgres descartável | SKIP pelos insumos indisponíveis descritos na seção 6 | — | — |

Tudo está nos worktrees `C:/TECH/BRAIN-worktrees/TASK-015/`. Nenhum commit, push, deploy, SQL remoto, alteração de ambiente ou cobrança foi feito. A revisão independente (`tasks/TASK-015/REVIEW.md`) encontrou sete pontos importantes; a correção e os testes de regressão constam dos resultados dos implementadores. O gate completo da API passou em quatro workers (`uv run --with pytest-xdist python -m pytest -n 4 -q -p no:cacheprovider`, 1224/3/0). Um teste legado de troca de assinatura foi atualizado para estabelecer a ID nova por `checkout.session.completed`, em acordo com a guarda contra eventos Stripe obsoletos; RED isolado antes, 31 testes focados verdes depois, suíte completa verde.

## 2. Modelo

`Entitlement.plan` ancora a secretarIA quando ela existe; caso contrário, representa o tier PreCheck ou `free`. `precheck_plan` guarda o tier PreCheck numa clínica dupla sem plano combo. `manual_products` identifica famílias concedidas fora do Stripe. `catalog.compose_entitlement_state` compõe os produtos, e `billing.apply_subscription_state` aplica assinatura e concessões manuais no webhook e na conciliação. Leitores de PreCheck usam `catalog.precheck_plan_of` para não inferir o tier de `plan` numa clínica dupla.

## 3. Contrato HTTP

`POST /billing/add-product` aceita `product` (`precheck` ou `secretaria`), `plan?`, `addons?`, `confirm`, `return_to?`, `expected_charge?` e `quote_token?`. Prévia (`confirm=false`) devolve `status=preview`, `charge` e um `quote_token` assinado de dez minutos; pode devolver `already_present` sem nova cobrança. Confirmação exige `Idempotency-Key` UUID. A UI envia o mesmo token, os quatro campos de `expected_charge` exibidos e a mesma chave em tentativas cujo resultado é incerto. O servidor fixa a data proporcional da prévia, compara valor e seleção antes de atualizar Stripe e recusa mudança com `preview_changed`. `return_to` só aceita valores da allowlist; `return_query` recompõe o retorno ao portal. `GET /entitlements` adiciona `precheck_plan`.

Recusas estáveis incluem 403 `billing_role_required`, `product_not_launched`, `test_tenant_billing_disabled`; 409 `no_active_subscription`, `subscription_past_due`, `subscription_trialing`, `product_already_active`, `subscription_changed`, `preview_changed`, `add_product_in_progress`, `payment_action_required`, `payment_method_required`, `subscription_shape_unsupported`; 402 `payment_failed`; 422 `idempotency_key_required`, `invalid_idempotency_key`, `preview_quote_required`, `preview_quote_invalid`, `preview_quote_expired`; 502 `preview_unavailable` ou `add_product_unconfirmed`; 503 `billing_not_configured` ou preço ausente. A API conserva `detail` como string. Formato de fatura não demonstravelmente exato falha fechado; `next_invoice_cents` fica nulo quando a renovação não é comprovável.

## 4. Decisões aprovadas

| Decisão | Implementação |
|---|---|
| D1: PreCheck flat proporcional agora; metered sem proporcional | `billing.proration_for_items`, `_update_form` |
| D2: assinatura em trial não recebe produto | `_add_product_preflight` |
| D3: sem desconto de combo na adição | seleção incremental e card do produto faltante |
| D4: escrita billing só por manager/owner | `deps.is_billing_manager`, `api.billing.require_billing_manager` |
| D5: preço exibido e prévia exata | `parse_preview_charge`, quote assinado, `AddProductDialog` |
| D6: secretarIA sujeita à flag de lançamento desligada por padrão | `BILLING_ADD_SECRETARIA_ENABLED`, `_add_product_preflight` |
| D7: trial somente quando devido | `_apply_trial` |
| D8: produto de cortesia pode converter-se em pago | `apply_subscription_state` |
| D9: preservar concessões manuais | `manual_products` e composição por família |

## 5. Desvios e correções de coerência

O ID da migração é curto para caber em `alembic_version.version_num`. Concessão manual só é consumida por assinatura viva. Combo de cortesia convertido para PreCheck pago vira componente manual `secretaria_basico`, sem manter PreCheck indevidamente após cancelamento. Eventos de outra assinatura não substituem uma assinatura viva; `checkout.session.completed` estabelece a nova ID antes do seu evento `created`. Webhook e endpoint fazem leitura bloqueada/fresca e o endpoint recusa resultado de assinatura que mudou durante a chamada Stripe. Assinatura cancelada preserva somente produtos manuais. `restart_test_window` não pausa cobrança ativa e recria os itens das duas famílias. `upgrade_precheck_plan` protege combo e clínica dupla. Formas de fatura Stripe com ajustes, paginação ou discriminação insuficiente retornam `preview_unavailable`.

## 6. Prova Stripe e Postgres

A autorização do dono de 2026-09-29 permite provas na conta Stripe real, mas nenhum passo foi executado nesta sessão: `STRIPE_SECRET_KEY`, `STRIPE_PRICE_MAP`, `STRIPE_PROOF_TENANT_ID` e `STRIPE_PROOF_CUSTOMER_ID` não estavam disponíveis no processo (somente presença booleana foi verificada). O daemon Docker também estava indisponível. Não se leu `.env` nem se registrou segredo. `scripts/stripe_proof_guard.py::run_money_step` tem testes locais para SKIP sem alvo e ABORTED quando o par tenant/cliente diverge. As fixtures de replay aguardam eventos reais sanitizados.

| Passo do plano | Classe | Resultado |
|---|---|
| 0: upgrade/downgrade em Postgres descartável | — | SKIP (auto): daemon Docker indisponível; SQLite testado |
| 1: prévia na assinatura designada | 1, sem cobrança | SKIP (auto): sem credenciais/preços e assinatura alvo |
| 2: adicionar e conferir fatura | 2, cobra | SKIP (auto): sem par designado |
| 3: cartão recusado e 3DS | 2, conta de teste | SKIP (auto): sem conta de teste/par designado |
| 4: test clock, renovação e cancelamento | 2, conta de teste | SKIP (auto): sem conta de teste/par designado |
| 5a: checkout de cortesia e expiração imediata | 1, sem cobrança | SKIP (auto): sem credenciais/preços |
| 5b: completar pagamento da cortesia | 2, cobra | SKIP (auto): sem par designado |
| 6: trial real | 2, conta de teste | SKIP (auto): sem conta de teste/par designado |
| 7a: idempotência e expiração de Checkout Session | 1, sem cobrança | SKIP (auto): sem credenciais/preços |
| 7b: idempotência do add-product cobrado | 2, cobra | SKIP (auto): sem par designado |
| 8: moeda e intervalo dos preços configurados | 1, leitura | SKIP (auto): sem credenciais/preços |
| 9: formato de invoice na versão real da conta | 1, leitura | SKIP (auto): sem credenciais/preços |
| 10a: Chrome local contra Stripe real, até a prévia | 1, sem cobrança | SKIP (auto): sem credenciais/preços; fixture de UI provada |
| 10b: Chrome local até confirmação e portal | 2, cobra | SKIP (auto): sem par designado; fixture de UI provada |
| 11: replay assinado de webhook real | 1, local | SKIP (auto): fixtures reais sanitizadas indisponíveis |
| 12: guarda do par financeiro | local | PROVADO: `tests/test_stripe_proof_guard.py` |

## 7. Ordem de implantação

Após autorização específica do dono: TASK B presente → migração `0026` pelo dono → brain-api com derivação, webhook e add-product → brain-frontend → Brain-Message-Frontend. Antes de liberar o fluxo de secretarIA, coordenar `PRODUCT_LAUNCHED` e `BILLING_ADD_SECRETARIA_ENABLED`. `NEXT_PUBLIC_BRAIN_MESSAGE_URL` e `STRIPE_CHECKOUT_SUCCESS_URL` precisam apontar para as origens de retorno aprovadas. Nenhum desses passos está autorizado por este registro.

## 8. Limitações

`POST /billing/checkout` mantém a lacuna do gate de lançamento existente, fora do D6 aprovado. Migração para combo, remoção de produto por UI, downgrade, cupons e múltiplas assinaturas ficam fora do plano. O repo não fixa `Stripe-Version`; compatibilidade com a fatura da conta real e semântica de lock do Postgres permanecem sem prova. Chamadores diretos legados sem quote continuam aceitos e não têm a garantia de consentimento exato da UI nova. A volta via Billing Portal usa a URL configurada do portal, fora de `return_to`.

## 9. Observabilidade

Monitorar `billing_add_product_previewed`, `_requested`, `_succeeded`, `_refused`, `_stripe_failed`, `_unconfirmed`, `stripe_subscription_families_derived`, `stripe_subscription_conflicting_plans` e `stripe_event_stale_subscription`. Investigar especialmente conflitos de plano, `add_product_unconfirmed`, `subscription_shape_unsupported` recorrente e eventos obsoletos repetidos; logs não devem conter PII, segredo ou payload Stripe.

## 10. Ações posteriores do dono

Aplicar a migração e implantar na ordem da seção 7 só mediante pedido explícito. Fornecer uma prévia Vercel, se quiser prova hospedada da UI. Executar as classes de prova Stripe quando houver entradas adequadas; passos que movem dinheiro exigem o par `STRIPE_PROOF_*` designado e passam pelo guarda. Sanitar eventos reais antes de criar fixtures de replay. Ver `tasks/TASK-015/TASK.md` para gates e estado mais recente.
