# TASK-054 — Equipe e primeiro acesso

Correção complementar de Contexto aprovada e validada localmente: resume sem cookie de setup retorna `200 {requires_password_change:false}`, sem credencial e sem tocar no cookie normal. Mantém CSRF e 401 para cookie inválido/expirado. Frontend anterior já tolera a ausência no formato JSON, permitindo rollout backend primeiro. Testes de autenticação relacionados: 44 passed. Detalhes em `C:/TECH/BRAIN/tasks/TASK-054/CONTEXT_FIX.md`. Integração e push para origin/main autorizados pelo usuário; sem deploy.

Integra??o e push para `origin/main` autorizados pelo usu?rio em 2026-10-12. Implementa??o validada nos worktrees TASK-054; deploy e migra??o remota ficam a cargo do usu?rio e n?o foram executados pelo agente.

## Comportamento

O diagnóstico real foi `409 already_bound`: a conta já tinha profissional, mas inativo e omitido pelo config-status. A opção `include_inactive=true` da listagem usa uma leitura interna fresca; a forma padrão permanece compatível. `/auth/me` expõe vínculo e flags, sem segredos. Self-bind/convites não podem tomar um profissional já vinculado; mutações são serializadas por tenant. Criação interativa respeita a capacidade da secretarIA; bootstrap não muda.

`api/team.py`, `schemas/team.py`, `services/team.py` administram somente a clínica da sessão. Proprietário/gestor ou médico gestor pode convidar médico, médico gestor, gestor sem atendimento e secretária. Gestor sem atendimento/secretária não ganha profissional agendável. Propriedade e última gestão ativa são protegidas. Revogação mantém agenda/histórico e bloqueia JWT, refresh e hub. Nenhum vínculo cross-clinic por e-mail.

## Autenticação e entrega

Migração aditiva `0029_staff_first_access`: obrigação/prazo de troca, versão de credenciais, revogação e status de entrega. Defaults preservam contas existentes. Senha temporária exclusiva de 72 horas, somente hash no usuário. Login emite `staff_password_setup` por no máximo 10 minutos, sem refresh operacional, cookie `__Host-staff_setup` separado e protegido. Resume exige CSRF; conclusão compare-and-swap exige senha diferente, respeita 72 bytes e emite sessão normal. Gates leem usuário/versão ao vivo. Impersonação não aceita pendente/revogado e usa a versão atual.

Configuração nova: `BRAIN_MESSAGE_PUBLIC_URL`, raiz do portal unificado, sem rota; `/login` é acrescentado no código (TASK-055), HTTPS fora de dev/test. URL inválida recusa convite antes de criar conta. Não usar `/conversa`. Template/worker da secretarIA cifram todo o envelope antes do Redis; callbacks são versionados. `queued` não significa `sent`; falha permite reenvio sem duplicar. Reenvio invalida senha/tickets anteriores. Links antigos e recuperação de senha permanecem compatíveis; criação legada de convites agora exige gestão.

Recuperação conclui a obrigação de troca e consome prova de recuperação/convites antigos em update atômico, com incremento SQL da versão. Não aceita a própria senha temporária como senha pessoal nem senha acima do limite por bytes. A prova antiga não pode ser reutilizada para acessar uma conta já recuperada. Testes focados: 25 passed com custo bcrypt normal.

## Validação e limites

Baseline, RED→GREEN e contagens consolidadas no TASK.md. Migração/downgrade/upgrade provados em PostgreSQL 16 descartável. Integração real com API, secretarIA, ARQ/Redis e SMTP capturador passou para quatro perfis, reenvio e revogação; relatório sem credenciais em `integration-local.json`. A varredura completa normal identificou fixtures que autenticavam usuários inexistentes; foram corrigidas. A varredura final reduz somente custo dos hashes de fixtures, preservando algoritmo/política e o default de produção (12).

Ruff foi bloqueado pelo Windows; não declarar lint aprovado. Sintaxe/diff/formatação verificados. Revogação bloqueia gateway/novos SSO, mas não encerra sessões nativas de PreCheck já emitidas. SMTP não é exatamente uma vez; senha antiga continua inválida após reenvio.

Publicação futura: migração → secretarIA API **e** worker → brain-api → frontend. Validar configuração por procedimento seguro, sem exportar variáveis. Rollback para código que ignore a obrigação de troca é inseguro após emitir senhas temporárias. Publicação não autorizada.
