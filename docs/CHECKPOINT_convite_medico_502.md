# Convite médico: recusa de plano mascarada como 502

Continuação da TASK-055 após o usuário relatar 502 no convite de médico. Correção local validada; envio a origin/main segue a autorização da tarefa. Sem deploy manual, leitura de variáveis do painel, criação de convite real ou envio de e-mail.

## Evidência de runtime disponível

Health público do brain-api e secretarIA respondeu 200, diretamente e pelo proxy do portal. /login respondeu 200. O source_fingerprint público da secretarIA foi `884334121bfc`, igual aos fontes Git LF da versão enviada. Esses checks não provam sucesso da operação de criação nem descartam falhas por requisição.

Não há navegador conectado nesta sessão. O MCP Easypanel está desativado e não há ferramenta allowlisted de logs disponível. A resposta da tentativa real não foi obtida; usuário informou que o JSON solicitado não aparece. Solicitado somente nome de evento/status do log do brain-api, sem segredos ou PII. A causa específica do 502 de produção continua sem confirmação.

## Defeito reproduzido no código

`services/secretaria_provisioning.py::_request` convertia todo 403 em None. `create_professional(interactive=True)` já tinha uma regra para propagar `multi_professional_not_entitled` ao portal, mas ela nunca recebia a resposta 403. `team.invite_team_member` convertia o None em 502 `secretaria_unavailable`, e o frontend exibia o fallback genérico.

Um teste com resposta 403 e esse código falhou antes da correção: não levantava a recusa esperada, e o log chamava o resultado de `secretaria_provisioning_unconfigured_upstream`.

## Correção

Somente a criação interativa habilita a preservação das duas recusas conhecidas (`multi_professional_not_entitled`, `professional_limit_reached`). 401, 403 desconhecido, HTML e JSON sem objeto continuam falhando fechados; bootstrap mantém sua política anterior. O frontend já possui mensagens específicas para os dois códigos; não foi necessário editar a interface.

Falhas de criação com resposta HTTP que antes eram silenciosas passam a emitir `secretaria_create_professional_failed`, com apenas `upstream_status`. Nenhum corpo, token, nome, e-mail ou identificador de tenant é acrescentado ao log.

## Validação e limite

71 testes de regressão, equipe e onboarding passaram. Após sugestão do reviewer, quatro casos de resposta 403 malformada foram acrescentados: os 12 testes específicos passaram, total de 75 casos distintos validados. Ruff e git diff check aprovados. Reviewer independente sem findings bloqueantes; bootstrap e autenticação preservados.

Essa correção elimina um caminho comprovado de 502 enganoso. Não afirma que o plano dessa clínica seja a causa da tentativa relatada; isso exige a resposta ou os logs da requisição real.
