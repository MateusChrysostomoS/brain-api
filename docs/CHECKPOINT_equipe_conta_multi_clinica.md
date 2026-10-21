# TASK-057 — conta pessoal de equipe e vínculos clínicos

2026-10-13. Implementação LOCAL no worktree `C:/TECH/BRAIN-worktrees/TASK-057/brain-api`, branch `task/TASK-057-staff-account`, base `176c541`. NÃO commitada, NÃO integrada, NÃO publicada. Revisão independente final **READY**, achados resolvidos e consolidação concluída. Nenhuma autorização de produção/Stripe/SQL remoto/commit foi usada.

Contrato para fases 2/3: [STAFF_ACCOUNT_CONTRACT.md](STAFF_ACCOUNT_CONTRACT.md). Fase 4 recebe apenas base pessoal, não checkout/criação de clínica. Registro central: `C:/TECH/BRAIN/tasks/TASK-057/TASK.md`.

## Implementado

- Conta pessoal com e-mail normalizado/credencial/versão/cota de recuperação; User continua identidade clínica com os mesmos IDs e permissões locais. Professional e PrecheckAccountLink não são recriados ou reatribuídos.
- Sessão pessoal HttpOnly distinta, JWT pessoal com assinatura derivada por propósito, listagem, seleção explícita, preferência no servidor, limpeza e logout. Token pessoal e novo setup não passam nos validadores da chave operacional compartilhada, inclusive consumidores permissivos do PreCheck.
- Convite direcionado de conta existente: vínculo pendente, token único 72h, sem trocar senha. Conta nova recebe senha temporária 72h/setup restrito. Escritores legados de convite usam o fluxo canônico quando a credencial pessoal é ativada; resposta conserva IDs/link, mas o link passa a ser login e a prova fica no e-mail. Provas antigas continuam no caminho compatível estreito.
- Recuperação request/verify-code/confirm preservada, SMTP no brain-api depois da resposta pública, código 6 dígitos/10min/5 tentativas, cotas configuradas, passe único 15min. Conta válida com todos os portais revogados recupera sem token clínico. Reset não restaura revogação/gestão nem aceita convite pendente.
- Geração de prova vincula emissão de refresh; login/convite anterior a reset não pode emitir refresh posterior com nova autoridade. Gates clínicos/hub leem vínculo e sessão ao vivo. Logout legado também encerra/limpa sessão pessoal; login operacional legado não herda cookie pessoal.
- Migração **0031 somente expande** após 0030: mantém unique global e credenciais/desafios legados intactos. **0032** transfere a versão mais recente após todos os escritores compatíveis, preserva provas/counters e retira unique com reservas/índices PostgreSQL. Duas flags default false. Rollback não pode dispensar revogação/versões/assinaturas restritas; ver contrato.

## Provas locais já executadas

- Baseline obrigatório de recuperação/mailer: 36 passed, bcrypt padrão.
- Varredura completa com bcrypt padrão após implementação principal: **1.483 passed, 8 skipped**, 39 warnings; `pytest-final-v3.log`. Depois houve ajustes de compatibilidade em cookies/convites e testes adicionais: validação focada abaixo. Não apresentar essa varredura como executada após alterações posteriores.
- Famílias auth/equipe/setup/recuperação/mailer/onboarding/secretária: **137 passed** em 235,48s; `pytest-auth-final.log`.
- Casos pessoais recentes e escritores de convite: **14 passed**, após mudanças de cookie/convites.
- PostgreSQL 16 descartável, loopback, conexões distintas: **7 passed**. Inclui 0030 real; expansão 0031 sem transferir estado; escritor legado altera senha/counters entre etapas; 0032 transfere a última versão; downgrade/re-upgrade; IDs/prova/cotas/PreCheck preservados; downgrade inseguro recusado; aceite concorrente único; seleção versus revogação; refresh clínico e pessoal/replay; geração nova sobrevive a replay antigo; e-mail normalizado concorrente.
- Ruff dos arquivos alterados passou; diff whitespace passou. Ruff completo tinha 10 erros no HEAD original; agora restam 8 em testes não alterados (imports/F811). Baseline, não declarar lint global verde.
- Brain-Message-Frontend: **2.110 testes**, typecheck e build passaram. brain-frontend: **278 testes**, tsc --noEmit e build passaram. Nenhum arquivo de frontend editado; instruções locais existentes preservadas.
- API real local + PostgreSQL + capturador SMTP real, sem serviços externos: recuperação completa iniciada nos **dois frontends**, volta ao login com aviso/e-mail; mesma credencial alterada; senha anterior recusada; A continua revogada; B permite só seu papel; token pessoal 401 em staff; logout invalida contexto selecionado. Repetido após assinatura por propósito. F5 no passo de senha do Brain-Message volta ao passo de e-mail; confirmação em ambos não fez login automático. Captura SMTP prova recebimento no capturador, não entrega externa.

Comandos principais (no worktree, `PYTHONPATH=src`, chave fictícia explícita nos testes finais):

```powershell
& C:/TECH/BRAIN/brain-api/.venv/Scripts/python.exe -m pytest -q --tb=short
& C:/TECH/BRAIN/brain-api/.venv/Scripts/python.exe -m pytest tests/test_staff_account_postgres.py -q --tb=short
& C:/TECH/BRAIN/brain-api/.venv/Scripts/ruff.exe check <arquivos-alterados>
git diff --check
```

PostgreSQL opt-in: `STAFF_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55457/task057_finalpg`. Fixture recusa host remoto e DB fora de `task057_*`. Container dedicado `brain-task-057-postgres`, sem volume de produção. Harness local `tests/staff_local_server.py`; SMTP 8057, API 8857, proxies 8957/8958. Logs/resultados ficam em TASK-057; não copiar tokens/senhas/e-mails reais para relatórios.

## Limites e pendências

Revisão independente concluída: contratos disponíveis para desenvolvimento local das fases 2/3. Nenhuma publicação parcial antes de consumidores compatíveis. Não houve implementação de telas, restauro nem checkout/criação autenticada de clínica.

### Retomada e correções da revisão

Primeira passada do revisor: CHANGES_REQUIRED, três achados registrados em `tasks/TASK-057/results/revisor-conta-clinica.md`. F1 (P1): convite/setup podiam reler uma geração posterior ao reset e emitir a partir de prova antiga; F2 (P2): replay clínico antigo revogava refresh novo; F3 (P2): privacidade omitira conta sem User. Testes PostgreSQL reais reproduziram emissão 200 em vez de 401, refresh novo recusado e export sem conta. O principal corrigiu os emissores para conservar a geração autorizada/resultante e revalidar a conta sob lock, recusou provas expiradas/antigas antes de replay destrutivo e adaptou export/apagamento à conta independente. O mesmo F1 foi reproduzido e corrigido no login legado account-aware.

Depois das correções F1–F3: **236 passed** nas famílias auth/refresh/setup/contas/convites/recuperação/mailer/privacidade/onboarding/team, bcrypt normal, `pytest-review-regression-final.log` (303,53s). Essa bateria antecede somente o ajuste adicional do login legado, que tem teste RED próprio e nova bateria focada. PostgreSQL final após esse ajuste: **16 passed**, `pytest-review-postgres-final-v2.log` (48,34s), incluindo janela após commit e após helper, reset concorrente com login, refresh null/expirado, recuperação com todos os vínculos revogados sem token clínico, e export/apagamento de conta sem vínculo. Os testes usam endpoints ASGI reais e conexões PostgreSQL distintas, sem stubs dos serviços de conta/recuperação.

Foco final após o último ajuste do login: **74 passed**, `pytest-review-login-green.log` (135,93s). Migrações após ajustes apenas de formatação/Ruff e quebras de SQL: **3 passed**, `pytest-review-migration-format.log` (13,50s). Ruff explícito de todos os arquivos Python alterados, incluindo as migrações novas, e diff-check passaram. Não há varredura completa posterior a estas mudanças; as baterias finais cobrem as famílias afetadas e as corridas corrigidas.

Confirmação independente final: **READY, F1/F2/F3 RESOLVED**, sem bloqueante adicional nos caminhos/locks corrigidos. Relatório na TASK-057; revisor encerrado. Não confundir essa numeração com os achados adiados da TASK-056. Resultados anteriores de frontend/navegador/SMTP mantêm seus escopos originais.

API/SMTP/proxies de teste encerrados e container PostgreSQL dedicado removido após confirmação de seu ID exato. Logs permanecem na task; não há banco descartável em execução. Recriar o ambiente local dedicado é necessário para repetir a bateria opt-in. HEADs originais revalidados, alterações do dono preservadas. Graphify permanece STALE com código não commitado e não foi usado como prova funcional.

Revogação/reset/logout não encerram sessões nativas PreCheck já emitidas nem fazem suas rotas Brain consultarem geração do brain-api. JWTs clínicos diretos persistem até expiração nesse consumidor. Novos tokens pessoais/setup recusam a assinatura no irmão; tickets legados conservam o limite pré-existente durante TTL curto. Nenhum código do PreCheck/WhatsApp foi modificado.

TASK-056 F3/F5/F7 continuam baseline. F4 segue no caminho legado; nova cota pessoal é serializada/retida para não duplicar por clínica. Jobs/callbacks legados não foram eliminados. Subagendas `59f260a` da SecretarIA permanecem código histórico integrado localmente, sem comprovação de deploy desta tarefa. Não há `/status` fornecido pela superfície desta sessão; goal criado sem token_budget, nenhuma estimativa tratada como saldo.
