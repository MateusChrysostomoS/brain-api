# Limpeza administrativa de pacientes por clínica — TASK-029

Integrado à main local em 2026-10-03. Sem push, deploy ou exclusão em produção.

## Operação pelo Swagger

Usar o login existente de administrador do Brain e autorizar seu JWT no Swagger. Ambas as rotas exigem `require_role("admin")`; usuário médico é recusado. Nenhum ADMIN_TOKEN é enviado pelo operador: as credenciais internas existentes são usadas exclusivamente pelo backend.

1. Identificar o UUID canônico da clínica no Brain. Nunca selecionar por nome aproximado.
2. Executar `GET /admin/tenants/{tenant_id}/patient-cleanup/preview`. Conferir `clinic_name`, contagens, `products`, `blockers` e `warnings`. A prévia não apaga registros.
3. Com atendimento/intake da clínica parado e recuperação dos bancos disponível, executar `POST /admin/tenants/{tenant_id}/patient-cleanup` com o nome EXATO devolvido pela prévia:

```json
{"confirm": true, "clinic_name": "Teste 2PRO Transition"}
```

4. Conferir `status=completed` e os resultados de cada produto. Repetir a prévia para verificar contagens zeradas. Repetir individualmente para GinecoAqui e Chrysostomo For Eyes usando seus próprios UUIDs e nomes.

O endpoint aceita uma clínica por chamada e é reutilizável para qualquer clínica; não existe autorização automática baseada nesses três nomes. Exclui TODOS os pacientes da clínica selecionada, inclusive eventuais pacientes reais. As contagens podem mudar entre prévia e execução; não há snapshot congelado nem bloqueio distribuído de novos atendimentos.

## Escopo e preservação

- Brain: pacientes, consentimentos e sessões/desafios de acesso vinculados. Contas compartilhadas com outras clínicas permanecem; ponteiros para a clínica removida são desvinculados. Contas sem outro vínculo são removidas.
- SecretarIA: pacientes, conversas, mensagens, mapas PII, consentimentos, reservas temporárias, recusas de remarcação e objetos R2 vinculados. Agendamentos locais são desvinculados e têm telefone/nome anonimizados; agenda e pagamentos permanecem.
- Precheck: pacientes, laudos/versões, mapas PII e auditoria antiga de pacientes/laudos em precheck_app; sessões, respostas, aprofundamentos, relatórios, triggers, mídia e eventos de mensagens em precheckv2. Objetos R2 vinculados são removidos.
- Clínicas, equipe, configurações, billing e outros tenants permanecem. Google Calendar, Asaas e relatórios exportados externamente não são alterados. Esta operação não é uma erasure de todos os sistemas externos.

Remove históricos repetidos já existentes. A causa da criação de conversas em novos logins não é alterada por esta entrega.

## Contrato e falhas

Resultado agregado: `ready`, `blocked`, `completed` ou `partial`; produto: `ready`, `completed`, `not_provisioned`, `blocked` ou `failed`. Clínica inexistente no Brain retorna 404; nome divergente retorna 409; confirmação falsa retorna 400. Erros de validação seguem FastAPI. Auditoria armazena ator, contagens e estados sem conteúdo de pacientes.

Antes de excluir, Brain repete a prévia dos produtos. Falta de configuração/schema/permissão impede começar. SecretarIA executa primeiro, Precheck depois e Brain por último. Não há transação distribuída: falha posterior pode deixar limpeza parcial. Brain preserva suas identidades enquanto algum produto não concluir. Corrigir o impedimento e repetir a mesma chamada é suportado; revisar o resultado também após timeout.

Anexos são apagados antes das linhas que guardam suas chaves. Falha de storage mantém referências para retry. Exclusões de objetos externos e commits entre bancos não têm rollback global.

## Pré-requisitos de publicação

Publicar os três serviços compatíveis antes de usar as rotas. Manter `SECRETARIA_BASE_URL`/`SECRETARIA_API_KEY` e `PRECHECK_BASE_URL`/`PRECHECK_INTERNAL_TOKEN` configurados no Brain. Nenhuma variável nova. Tokens nunca entram em payloads/respostas/logs.

Precheck exige acesso a ambos os bancos e SELECT/DELETE nas tabelas operacionais reconhecidas. O papel atual pode ser somente leitura; a prévia acusa `operational_delete_permission_missing`. Não foram feitos GRANTs nem mudanças de schema. Ver checkpoint do Precheck para revisão manual dessas permissões. Armazenamento R2 precisa estar configurado se houver anexos.

## Validação e limites

Testes de autenticação, confirmação, isolamento entre clínicas, contas compartilhadas, falha parcial, idempotência e contrato HTTP com transportes locais. Bancos de teste SQLite com FKs; PostgreSQL real, grants efetivos, UUIDs das clínicas, infraestrutura e exclusão em produção não foram validados. Consultar TASK-029/REVIEW.md para resultados das suítes e comparação com baseline.
