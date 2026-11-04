# CHECKPOINT — e-mail operacional da clínica pelo admin

Estado: **implementado LOCAL, sem commit, sem deploy, sem migração.** Par na secretarIA (rotas
internas) e no brain-frontend (tela). O e-mail mora na secretarIA (`Tenant.contact_email`); o
brain-api **não** guarda cópia (nenhuma coluna nova em `Tenant`).

## Contrato HTTP

Ambas atrás do `require_role("admin")` do router `/admin` (`api/admin.py`). Não-admin = 403, sem
token = 401.

`GET /admin/tenants/{tenant_id}/contact-email` → 200

```json
{ "tenant_id": "8f0c2c0e-…", "contact_email": "contato@clinica.com" }
```

`contact_email` é `null` quando a clínica não tem e-mail cadastrado.

`PATCH /admin/tenants/{tenant_id}/contact-email` — corpo fechado (`extra="forbid"`), a chave é
**obrigatória** (corpo `{}` = 422, para um bug de cliente nunca apagar o e-mail):

```json
{ "contact_email": "  Contato@Clinica.COM " }
```

O valor é aparado e passado a minúsculas antes de validar (`EmailStr`, máx. 320). `null`, `""` e
texto só de espaços **limpam** o e-mail. Resposta 200 no mesmo formato do GET, com o valor que a
secretarIA devolveu:

```json
{ "tenant_id": "8f0c2c0e-…", "contact_email": "contato@clinica.com" }
```

## Erros

| Situação | Status | `detail` |
|---|---|---|
| Clínica desconhecida no brain-api (a secretarIA nem é chamada) | 404 | `Tenant not found` |
| secretarIA responde 404 (clínica só do PreCheck, sem linha lá) | 404 | `secretaria_tenant_not_found` |
| secretarIA sem configuração, fora do ar, 401/403, 5xx ou corpo ilegível | 502 | `secretaria_unavailable` |
| secretarIA responde 422 (e-mail recusado) | 422 | `invalid_contact_email` |
| Corpo sem a chave, e-mail inválido, tipo errado ou campo extra | 422 | erro de validação do FastAPI |
| Sem papel `admin` / sem token | 403 / 401 | — |

Atenção: dois 404 distintos — o front deve ler o `detail` (clínica sem secretarIA ≠ clínica
inexistente).

## Rotas internas consumidas (secretarIA, `X-Internal-Api-Key`)

- `GET   /internal/tenants/{id}/contact-email` → 200 `{"contact_email": str|null}`; 404.
- `PATCH /internal/tenants/{id}/contact-email` com `{"contact_email": str|null}` → 200 igual; 404; 422.

Cliente: `services/secretaria_provisioning.py::get_contact_email` / `set_contact_email`, que
devolvem um `ContactEmailResult` etiquetado (`ok | not_found | invalid | unavailable`) e nunca
levantam. O mapeamento para HTTP está em `services/admin.py::get_contact_email` /
`set_contact_email` (que antes confere o tenant no banco do brain-api).

## Privacidade

O PATCH registra `admin_contact_email_updated` com `actor_user_id`, `tenant_id` e `cleared`
(booleano). O endereço **nunca** vai para log — nem no sucesso, nem nos avisos de falha do cliente.

## Testes

`tests/test_admin_contact_email.py` (27 casos; a chamada de rede é trocada em
`secretaria_provisioning._request`): GET/PATCH felizes, normalização, limpar (`null`/`""`/espaços),
corpo inválido sem chamar a secretarIA, 404 nos dois sentidos, 502 (sem resposta, 500, 503 HTML,
corpo não-JSON/inesperado e malha sem configuração pelo `_request` real), 422 da secretarIA, RBAC
(403/401) e ausência do e-mail nos logs.

```
uv run pytest tests/test_admin_contact_email.py
uv run ruff check src/brain_api/api/admin.py src/brain_api/services/admin.py \
  src/brain_api/services/secretaria_provisioning.py src/brain_api/schemas/admin.py \
  tests/test_admin_contact_email.py
```

Não provado: contra a secretarIA real (outra sessão a constrói contra o mesmo contrato).

## Ordem de deploy

secretarIA primeiro (rotas internas) → brain-api (este) → brain-frontend. Sem migração. Se este
for ao ar antes da secretarIA, as duas rotas respondem 404 `secretaria_tenant_not_found` ou 502
`secretaria_unavailable`; nada quebra.
