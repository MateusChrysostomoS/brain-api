-- ============================================================================
-- provision_precheck_tenants.sql
-- Alinha as clínicas do PreCheck como tenants na brain-api.
--
-- CONTEXTO: a brain-api NÃO tem endpoint para criar tenant (só seed/insert direto),
-- e não há sync automático PreCheck -> brain. Este script faz o bootstrap manual.
--
-- Os dois bancos são instâncias Postgres SEPARADAS (não dá para fazer JOIN entre elas),
-- então o fluxo é em fases: o PreCheck GERA o SQL da brain como texto, você copia a
-- saída e cola no console da brain. O UUID do tenant nasce no PreCheck (coluna
-- clinics.brain_tenant_id), garantindo que a ligação bata dos dois lados.
--
-- ORDEM: Fase 1 (PreCheck) -> Fase 2 (brain) -> Fase 3 (PreCheck, opcional) -> verificação.
-- ============================================================================


-- ============================================================================
-- FASE 1 — rodar no console do Postgres do PRECHECK
-- ============================================================================

-- 1a. pgcrypto fornece gen_random_uuid() (necessário em PG < 13; inofensivo em PG 13+).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1b. Diagnóstico: clínicas ATIVAS sem um usuário admin (owner) no PreCheck.
--     Essas NÃO entram na geração abaixo — você precisa criar um owner para elas
--     manualmente na brain (via POST /admin/users) depois de criar o tenant.
SELECT c.id, c.name, c.slug
FROM clinics c
WHERE c.is_active
  AND NOT EXISTS (SELECT 1 FROM users u WHERE u.clinic_id = c.id AND u.role = 'admin')
ORDER BY c.id;

-- 1c. Atribui um UUID de tenant da brain a cada clínica ativa que ainda não tem.
--     Idempotente: só toca em quem está com brain_tenant_id NULL.
UPDATE clinics
SET brain_tenant_id = gen_random_uuid()::text
WHERE brain_tenant_id IS NULL
  AND is_active;

-- 1d. GERA o SQL da brain. Copie o conteúdo da coluna "brain_sql" e rode na FASE 2.
--     Pega UM admin por clínica (o de menor id) como doctor com is_owner/is_manager=true
--     (role-taxonomy: tenant_owner virou doctor + is_owner, ver migrations/0012).
--     Senha temporária = 'CHANGE_ME_123' (veja a nota sobre senha no fim do arquivo).
SELECT string_agg(stmt, E'\n\n') AS brain_sql
FROM (
  SELECT DISTINCT ON (c.id)
      format(
        'INSERT INTO tenants (id, clinic_name) VALUES (%L::uuid, %L) ON CONFLICT (id) DO NOTHING;',
        c.brain_tenant_id, c.name
      )
      || E'\n' ||
      format(
        'INSERT INTO users (id, tenant_id, email, name, password_hash, role, is_owner, is_manager) '
        'VALUES (gen_random_uuid(), %L::uuid, %L, %L, crypt(''CHANGE_ME_123'', gen_salt(''bf'', 12)), ''doctor'', true, true) '
        'ON CONFLICT (email) DO NOTHING;',
        c.brain_tenant_id, lower(u.email), COALESCE(u.name, c.name)
      )
      || E'\n' ||
      format(
        'INSERT INTO entitlements (tenant_id, precheck_enabled, plan, status) '
        'VALUES (%L::uuid, true, ''brain-completo'', ''active'') '
        'ON CONFLICT (tenant_id) DO UPDATE SET precheck_enabled = true;',
        c.brain_tenant_id
      ) AS stmt
  FROM clinics c
  JOIN users u ON u.clinic_id = c.id AND u.role = 'admin'
  WHERE c.brain_tenant_id IS NOT NULL
    AND c.is_active
  ORDER BY c.id, u.id
) s;


-- ============================================================================
-- FASE 2 — rodar no console do Postgres da BRAIN-API
-- ============================================================================

-- 2a. pgcrypto fornece crypt()/gen_salt('bf') (hash bcrypt compatível com o passlib
--     da brain) e gen_random_uuid(). Rode ANTES de colar o SQL gerado.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 2b. Cole aqui a saída "brain_sql" da FASE 1 (1d) e execute.
--     (Os INSERTs são idempotentes: ON CONFLICT protege reexecução.)
--     >>> COLE AQUI <<<


-- ============================================================================
-- FASE 3 — (opcional) rodar no console do PRECHECK
-- ============================================================================
-- A FASE 1c já preencheu clinics.brain_tenant_id, que é EXATAMENTE o que as rotas
-- /api/v1/doctor/anamneses do PreCheck usam para escopar os dados por tenant.
-- Nada mais a fazer aqui — a ligação já está feita. (Sem esse campo, o Modo médico
-- entra mas vem vazio de dados do PreCheck.)


-- ============================================================================
-- VERIFICAÇÃO
-- ============================================================================

-- Na BRAIN: confirmar tenants + owners + produtos ativos.
--   SELECT t.id, t.clinic_name, u.email, u.role, u.is_owner, e.precheck_enabled, e.status
--   FROM tenants t
--   LEFT JOIN users u ON u.tenant_id = t.id AND u.role = 'doctor' AND u.is_owner
--   LEFT JOIN entitlements e ON e.tenant_id = t.id
--   ORDER BY t.created_at;

-- No PRECHECK: confirmar a ligação.
--   SELECT id, name, brain_tenant_id FROM clinics WHERE brain_tenant_id IS NOT NULL ORDER BY id;

-- Escolha o e-mail de um owner acima, ponha em IMPERSONATION_DEMO_EMAIL na brain-api
-- (EasyPanel) e REINICIE o serviço — o "Modo médico" passa a mintar o token desse owner.


-- ============================================================================
-- NOTA SOBRE SENHA
-- ============================================================================
-- 'CHANGE_ME_123' é irrelevante para o "Modo médico": aquele fluxo minta o token do
-- owner no servidor (POST /admin/impersonate/token), SEM usar senha. A senha só
-- importa se esse médico for logar diretamente no portal com e-mail+senha — nesse
-- caso, troque-a antes (ou use um reset de senha). Todos os owners criados aqui
-- compartilham a mesma senha temporária.
