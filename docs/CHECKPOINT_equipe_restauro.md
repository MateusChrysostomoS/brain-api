# TASK-058 — Restauro do vínculo clínico (fase 2)

Estado: FASE 2 IMPLEMENTADA E VALIDADA LOCALMENTE, não commitada, não integrada, não publicada. Aceite visual conjunto aguarda fase 3. Base: snapshot da TASK-057 revisada READY, HEAD 176c541 + alterações locais listadas em tasks/TASK-058/base-snapshot.json. Worktree C:/TECH/BRAIN-worktrees/TASK-058/brain-api. Somente um agente; nenhum subagente.

## Contrato e estados

[Contrato aditivo de restauro](TEAM_RESTORE_CONTRACT.md). Não altera o contrato pessoal congelado da fase 1 nem os fluxos de recuperação TASK-056.

| Alvo | Efeito do restauro |
|---|---|
| Médico revogado, senha pessoal definida | Acesso ativo, is_manager=false; gestão só por concessão explícita posterior |
| Gestor sem atendimento revogado | Mesmo perfil de gestor, nenhum profissional criado |
| Secretária revogada | Mesmo perfil de recepção, nenhum profissional criado |
| Convite pendente para conta estabelecida | Continua pendente; novo convite enviado automaticamente, exige aceite; senha pessoal preservada |
| Primeiro acesso legado sem conta pessoal | E-mail reenviado automaticamente com nova senha temporária; exige troca obrigatória |
| Conta pessoal que nunca definiu senha | E-mail reenviado automaticamente com nova temporária de 72h; antiga e setup antigos inválidos; exige troca obrigatória |
| Proprietário | 409 clinic_owner_protected |
| Vínculo estrangeiro ou inexistente | 404 Team member not found |
| Operador sem gestão na clínica | 403 (ou 401 para geração/sessão obsoleta) |
| Confirmação obsoleta após nova revogação | 409 team_member_changed |

A lacuna de primeiro acesso foi resolvida por decisão posterior explícita do dono: “na hora que essa pessoa receber o acesso novamente, é necessário mandar o mesmo email novamente”. O e-mail mantém modelo/remetente/destinatário, com nova senha temporária somente para quem nunca criou a pessoal. A senha pessoal já estabelecida e os desafios de recuperação permanecem preservados. A implementação continua nos arquivos de equipe autorizados; não foi necessário alterar auth/setup/recuperação.

## Implementação

api/team, schemas/team e services/team: restauro por gestor da clínica corrente, lock de clínica e alvo relido, comparação condicional da geração, incremento somente na transição e revogação do refresh clínico. Restauro ativo repetido não altera flags/senha nem gira geração. Médico sempre perde gestão na transição de restauro; gestor sem atendimento mantém seu perfil. Flags da conta ou de B não conferem gestão em A.

Gestão agora incrementa a geração também em pendentes: tickets/convites anteriores ficam obsoletos, exigindo nova prova/reenvio. Reenvio recusa revogado antes de tratar membership_pending. Revogação de convite legado retém a obrigação de primeiro acesso antes de descartar o link. Restauro reenvia automaticamente a prova pendente; apenas a credencial temporária nunca concluída pode ser renovada. Senha pessoal já definida e desafios pessoais não são alterados. Proprietário e última gestão permanecem protegidos.

Nenhum código de auth, sessão pessoal, recuperação, modelo ou migração foi editado nesta fase. Nenhuma criação de profissional, Calendar, subagenda ou chamada ao serviço clínico foi adicionada ao restauro. User.id e professional_id preservados nos testes. A continuação provou também a preservação dos registros reais locais da SecretarIA, conforme evidência abaixo. Limites de revogação do PreCheck descritos na fase 1 continuam existentes.

## Evidências

- Baseline antes da edição: 12 testes de equipe/convites e 2 testes visuais estáticos passaram. RED: 5 testes de restauro responderam 404; frontend falhou pela ausência de Devolver acesso. Logs red-api.log e red-ui.log em tasks/TASK-058.
- Regressão final focada: 73 passed, 119,79s, custo bcrypt normal. Abrange equipe, entrega, restauro, convites pessoais, sessões pessoais, recuperação, primeiro acesso, reset e RBAC. Não é uma nova execução da suíte completa da fase 1.
- PostgreSQL 16 descartável local, migrações reais até 0032: 3 passed com conexões independentes. Dois restauros produzem uma geração; revogação aguarda restauro, mantém revogação final e recusa retry obsoleto; dois gestores não conseguem retirar simultaneamente a última gestão. Teste opt-in TEAM_TEST_POSTGRES_URL, somente host loopback e banco task058_.
- API HTTP real local + SMTP capturador: 20 verificações passaram (api-local-acceptance.json). Recuperar → autenticar pessoalmente com nova senha e ver A revogada no contrato → selecionar A negado → restaurar → selecionar/operar A autorizado; B continua viva durante a alteração em A; tokens antigos e refresh legado continuam inválidos; médico não administra até concessão separada. Captura SMTP local não comprova envio/recebimento externo.
- Continuação: 28 verificações de persistência clínica real local passaram (clinical-local-acceptance.json). PostgreSQL descartável, bancos separados brain-api/SecretarIA, modelos reais da SecretarIA em 59f260a e router interno real servido em loopback. Dois tenants, dois profissionais ativos com subagendas existentes e horários, dois pacientes/conversas/mensagens fictícios e quatro consultas (confirmadas/atendidas). Comparação de todas as colunas dessas seis tabelas antes/depois de gestão, revogação, restauro, retry e seleção A/B: idênticas. A API real de roster confirmou os mesmos IDs; middleware apenas de observação contou zero requisições de mutação clínica. brain-api estava configurado com upstream local real, sem stub. Schema clínico criado pelo metadata atual; isso não é prova de migração da SecretarIA ou de chamadas ao Google. Nenhum código/fonte da SecretarIA foi editado.
- Frontend: 2117 testes; typecheck e static build. Evidência final nos logs vitest-final/typecheck-final/build-final. Avisos existentes: Vite CJS e rewrites em export estático. Ruff dos sete Python da fase e diff-check passaram.
- Chrome com TeamSection/TeamMemberActions reais, client real e API real PostgreSQL, harness apenas local sem rota adicionada ao produto. Gestão removida/concedida, revogação e devolução confirmadas. Capturas exibidas na conversa: desktop/mobile, claro/escuro. Salvamento de screenshots no diretório foi recusado pela ferramenta; bloqueio respeitado, sem arquivo PNG persistido.
- Posições finais medidas: desktop 1440 px, slots management/access/resend x=253/567/881, y=316, 306x44; mobile 375 px, x=33, y=486/538/590, 310x44. Coordenadas iguais entre revogado e restaurado. Sem overflow horizontal. Teclado chega a Devolver acesso, Tab chega a Confirmar, Enter restaura e foco retorna ao slot Revogar acesso. Gestão indisponível fica disabled; espaços invisíveis não recebem Tab e são aria-hidden.

## Pendências e publicação

1. RESOLVIDO: reenvio automático de primeiro acesso aprovado e implementado, com temporária nova somente para quem nunca definiu senha pessoal. Última validação: 76 testes API, 4 PostgreSQL real, 37 verificações API/SMTP reais locais, 2117 frontend e typecheck/build, Ruff/diff-check. Logs auto-email-*. Falha SMTP real reporta failed e mantém pendência; retry da confirmação não duplica envio.
2. Integrar localmente com o trabalho do prompt 3 quando disponível, sem merge/commit automático: recuperar → login/card revogado → restauro → login/seletor real. Esta base não contém essas telas novas; o contrato foi testado, o card não foi fabricado.
3. Prova de persistência clínica local concluída na continuação, incluindo google_calendar_id armazenado; não houve acesso ao Google nem a dados de clínica real.
4. Nenhum resultado autoriza publicar ou SQL remoto. Os originais e TASK-057 foram preservados; apenas worktrees TASK-058 receberam esta implementação. Recursos locais de teste encerrados ao término da sessão (ver TASK.md para resultado final).
