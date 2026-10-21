# Contrato aditivo — restauro de equipe TASK-058

Estado LOCAL validado para a fase 2, sem publicação; aceite visual conjunto depende da fase 3. Complementa STAFF_ACCOUNT_CONTRACT.md sem alterar rotas de recuperação.

## Resposta administrativa

TeamMember acrescenta access_version (inteiro >=0): geração clínica do vínculo. Consumidores anteriores podem ignorar esse campo. Listagem e respostas de mutação continuam retornando os demais campos/status existentes.

## POST /doctor/team/{user_id}/restore

Bearer clínico vivo de gestor da própria clínica. Corpo obrigatório com extra=forbid: {"expected_version": 3}. Não aceita tenant, papel, profissional, senha ou privilégios. Inteiro estrito e não negativo. Tenant vem da sessão revalidada, alvo é resolvido dentro dela.

Retorna 200 TeamMember. Revogado com geração igual: incrementa geração clínica, limpa revogação/provas legadas, revoga refresh local e, se médico, is_manager=false. Outros perfis mantêm papel e profissional existente (inclusive null). Preserva senha pessoal já definida, preferências, recuperação e outros vínculos.

Decisão explícita posterior do dono: ao restaurar pessoa que ainda não concluiu sua entrada, reenviar automaticamente o mesmo e-mail de primeiro acesso/convite. Se nunca definiu senha pessoal, gera nova senha temporária de 72h, gira a geração da credencial temporária e mantém obrigação de troca. Vale também para temporária expirada. Senha temporária e setup anteriores não funcionam. Se a conta já possui senha pessoal e o vínculo é convite pendente, preserva essa senha e renova apenas o convite. O aceite continua obrigatório. Conta com senha pessoal definida e vínculo já concluído não recebe nova senha temporária.

Envio ocorre após commit, pelo mailer existente brain-api. Resposta reflete delivery_status sent/failed; falha não concede sessão operacional nem desfaz a pendência, e permite usar Reenviar primeiro acesso. Retry da mesma confirmação não gira outra senha nem envia outro e-mail. Não é garantia de entrega exatamente uma vez em SMTP, nem de recebimento externo.

Idempotência: ativo com geração atual igual à esperada ou esperada+1 retorna sem mutação; não remove gestão concedida separadamente. Geração posterior incompatível responde 409 team_member_changed, inclusive após nova revogação. Lock de clínica/alvo e UPDATE condicional impedem transição a partir de uma geração obsoleta.

Erros: 401 sessão/geração do operador inválida; 403 team_manager_required; 404 Team member not found para alvo estrangeiro/inexistente; 409 clinic_owner_protected; 409 team_member_changed; 409 account_unavailable para conta indisponível; 409 first_access_not_pending para obrigação inconsistente/convite ausente; 422 corpo inválido.

O bloqueio anterior first_access_restoration_requires_contract foi removido após essa decisão. Locks seguem account → clinic → membership, com releitura após adoção e comparação condicional da geração. Nenhum arquivo auth/setup/recuperação/modelo foi alterado. Publicação permanece não autorizada.

## Gestão e reenvio

PATCH management continua sendo concessão/retirada separada. Toda mudança efetiva gira geração também em pendentes; provas antigas exigem reenvio. POST first-access/resend não restaura revogado, inclusive membership_pending. O e-mail vem do mailer brain-api com sent/failed; não há ARQ ou link de reset. Perfil de gestor sem atendimento continua gestor, não vira médico.

## Consumidor

Devolver acesso ocupa o mesmo slot de Revogar acesso. Médico revogado mostra gestão desabilitada; após restauro pode Tornar gestor(a). Corpo usa access_version recebido da lista. Se campo estiver ausente (backend anterior), restauro permanece desabilitado até atualização compatível. Respostas/erros atrasados só se aplicam ao tenant/user original da tela.
