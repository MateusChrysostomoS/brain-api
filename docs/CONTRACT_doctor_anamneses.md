<!-- docs/CONTRACT_doctor_anamneses.md -->
# Contrato — `/doctor/anamneses*` (brain-api → PreCheck)

Estado: TASK-011 (2026-09-29), commitado local, **não deployado**. Consumidor: Brain-Message-Frontend
(`lib/real/console-api.real.ts`). Todas as rotas: JWT do Brain do usuário (repassado verbatim ao PreCheck,
que revalida), `require_doctor` no router e `deny_secretary(principal, "secretary_precheck_not_allowed")`
em cada rota. Token de `admin` de plataforma: 403 no `require_doctor` (portal errado).

| Rota brain-api | Upstream PreCheck | Sem `PRECHECK_BASE_URL` | Resposta |
|---|---|---|---|
| `GET /doctor/anamneses?skip&limit` | `GET /api/v1/doctor/anamneses` | `{items: [], total: 0, skip, limit, stub: true}` | lista paginada, preview sem PHI |
| `GET /doctor/anamneses/{id}` | `GET /api/v1/doctor/anamneses/{id}` | 503 `precheck_not_configured` | detalhe + `patient_id` + `patient {full_name, birth_date, sex, external_id}` (desde TASK-011; campos podem ser `null`; PreCheck antigo não manda o bloco) |
| `GET /doctor/anamneses/{id}/media` | `GET /api/v1/doctor/anamneses/{id}/media` | `{items: [], stub: true}` | `{items: [{media_id, type, kind: "image"\|"pdf", media_role}]}` |
| `GET /doctor/anamneses/media/{media_id}/url` | `GET /api/v1/doctor/anamneses/media/{media_id}/url` | 503 `precheck_not_configured` | `{url, expires_at}` — URL assinada do R2, ~15 min; o brain-api **não** baixa o arquivo e **não** loga a URL |
| `PATCH /doctor/anamneses/{id}/status` body `{status: "approved"\|"rejected"}` | idem, mesmo corpo | 503 `precheck_not_configured` | `{id, status, updated_at}`; 422 local para qualquer outro corpo (campo extra incluso), sem chamar o PreCheck |

Erros: 4xx do PreCheck atravessam com o mesmo status e `detail` (404 = fora da clínica do token ou inexistente —
indistinguíveis de propósito); 5xx e falha de rede viram 502 `precheck unavailable`.

O PATCH grava **só** `summaries.status` no PreCheck (+ `updated_at` do `onupdate`), nunca `reviewed_by_id`,
`structured_data`, `ai_summary` ou `final_summary`; audita em `audit_logs` (`brain_set_anamnesis_status`).
Detalhe do lado PreCheck: `PreCheck/docs/backend.md` §7 (tabela `brain`).
