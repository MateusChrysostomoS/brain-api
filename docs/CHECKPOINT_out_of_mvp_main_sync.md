# Atualização de feature/out-of-mvp — TASK-066

Em 2026-10-21, incorporada a main local `8655cec` na feature que partiu de `a00b2ff`, por pedido explícito de `git merge main`. Sem push/deploy.

Os tokens do hub mantêm role/is_owner/is_manager da TASK-020 e passam a carregar também credential_version/account_sid da main. A introspecção preserva as verificações atuais de vínculo, conta, revogação e troca obrigatória de senha. Testes antigos de role agora usam um ator existente, sem enfraquecer o controle de acesso.

Validação: 72 testes de emissão/introspecção/papel/escopo de agenda passaram; 26 testes adicionais de primeiro acesso, sessões de equipe e gestão passaram. Dois testes novos de integração do merge provam que tokens com papel deixam de funcionar após revogação do integrante ou mudança da versão da credencial (arquivo de emissão repetido: 9 passed). Ruff passou nos arquivos conciliados. Alembic tem um único head (`0032_staff_membership_email`); nenhuma migração executada. A suíte completa não foi concluída nesta tarefa.

O recurso exclusivo é a propagação das permissões para as operações de pagamento da secretarIA. A integração interna para descobrir se há secretária na clínica, usada pelos prazos da TASK-052, ainda falta. A gestão de equipe já presente na main incorporada permite retomar essa dependência.

A comparação usa main local. A referência origin/main já conhecida localmente estava três commits adiante; não foi feito fetch, atualização de main nem merge de origin/main. Relatório transversal: `C:/TECH/BRAIN/tasks/TASK-066/ANALISE.md`.
