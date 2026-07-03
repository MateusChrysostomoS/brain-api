# Cross-database LGPD erasure & portability — design

## The problem

Three FastAPI services — `brain-api`, `secretaria`, `precheck` — each own a **separate**
Postgres database. There is **no cross-database foreign key and no cascade** between
them (Postgres cannot enforce a constraint across two connections, let alone two
instances). A person's data can legitimately live in all three at once: a brain portal
user row + tenant membership (`brain`), WhatsApp conversation/appointment history
(`secretaria`), and a pre-consultation anamnesis (`precheck`).

LGPD (Brazil's data-protection law, the local analogue of GDPR) gives a data subject the
right to **erasure** ("esquecimento") and to **portability/export** of their own data.
Honoring either right therefore means touching three independent databases in one
logical operation — with no distributed transaction available (each service's own DB
transaction is local; there is no two-phase commit across the mesh). `brain-api`, as the
identity authority and the only service with a "who is asking" concept, is the natural
**orchestrator**: it does not (and cannot) reach into the other two databases directly —
it calls each service's own internal HTTP surface and lets that service erase/export
**its own** rows.

## Roles

| service | owns | erasure means | export means |
|---|---|---|---|
| `brain-api` | `users`, `demo_requests` (leads), `refresh_tokens`, `precheck_account_links` | delete the `users` row (cascades to `refresh_tokens` + `precheck_account_links`) and any `demo_requests` rows by email | the user's identity fields (no `password_hash`) + tenant name + their `demo_requests` |
| `secretaria` | patients, conversations, messages, appointments (WhatsApp-derived, keyed by `wa_id` within a tenant) | deletes the patient's conversation/message/appointment history for that tenant | the same, as a bundle |
| `precheck` | anamneses / clinical pre-consultation records, keyed by email and/or phone | **may anonymize rather than hard-delete** clinical records it is legally obligated to retain (CFM record-retention rules — see PreCheck's own `docs/legal`); a hard delete is not always the correct LGPD response for a clinical record | the same, as a bundle |

`brain-api` never assumes what "erased" means inside another service — it treats
`{"erased": {...counts...}}` as an opaque confirmation. Whether precheck deletes or
anonymizes a given row is precheck's own legal call, out of brain-api's scope.

## Orchestration flow

`POST /admin/privacy/erasure` / `POST /admin/privacy/export` (admin-gated,
`api/privacy.py` + `services/privacy.py`):

1. Validate the request: `confirm: true` (else `400`, before any work); a coherent
   subject — `subject_type="user"` needs `email`, `subject_type="patient"` needs
   `wa_id` + `tenant_id` (else `422`).
2. **Brain leg first, synchronously, in this request's own DB transaction.** For an
   erasure this is the ONLY leg that can refuse the whole operation: a target that is a
   platform `admin` is refused with `409 admin_cannot_be_erased` **before** any upstream
   call and **before** the audit row is written — an admin account can never be wiped
   through this path, and a refusal is never mis-logged as a (partial) privacy action.
3. **Fan out to secretaria and precheck**, each over its own internal service-to-service
   surface (see below). Each call returns either that service's own JSON body, or one of
   three non-raising markers — a failing/unconfigured/inapplicable leg **never blocks
   the others**:
   - `{"status": "failed"}` — network error, or the upstream answered 4xx/5xx.
   - `{"status": "skipped_unconfigured"}` — the base URL / shared key / token is unset
     on brain-api's side for that service.
   - `{"status": "not_applicable"}` — **secretaria only**, when the subject has no
     `wa_id`/`tenant_id` (a `subject_type="user"` request: secretaria has no notion of a
     brain user, only of patients keyed by `wa_id`, so there is nothing to call). This
     is expected, not a configuration problem, and does not affect the overall result.
4. Persist **exactly one** `privacy_requests` row (§ below) recording the outcome.
5. Respond `200` with `{"status": "completed" | "partial", "brain": {...},
   "secretaria": {...}, "precheck": {...}, "request_id": <uuid>}`.

## The fixed upstream contract

- **secretaria** (`X-Internal-Api-Key` = `SECRETARIA_API_KEY`, base
  `SECRETARIA_BASE_URL` — the SAME pair key already used for the doctor
  appointments/patients data path, CONTRACTS.md §12.1):
  - `GET /internal/privacy/tenants/{tenant_id}/subjects/{wa_id}/export` → `200` bundle.
  - `DELETE /internal/privacy/tenants/{tenant_id}/subjects/{wa_id}` → `200
    {"erased": {"patients": n, "conversations": n, "messages": n, "appointments": n}}`.
- **precheck** (`X-Internal-Token` = `PRECHECK_INTERNAL_TOKEN`, base
  `PRECHECK_BASE_URL` — a NEW, separate service credential; NOT the forwarded-brain-JWT
  proxy pattern used elsewhere for precheck, CONTRACTS.md §11.1):
  - `POST /api/v1/internal/privacy/export {"email": str|null, "patient_phone": str|null}`
    → `200` bundle.
  - `POST /api/v1/internal/privacy/erase` (same body) → `200 {"erased": {...counts...}}`.

## Idempotency & partial-result semantics

The whole flow is **idempotent**: re-running an erasure for an already-erased subject
re-executes the same local deletes (which match zero rows) and the same upstream calls
(which — because secretaria/precheck are themselves idempotent for a subject with no
remaining data — report zero counts). A `"completed"` response the second time simply
confirms there was nothing left to erase.

`status: "partial"` (still `HTTP 200`, never an error) means at least one upstream leg
failed or was unconfigured — the caller (a human admin, or automation) **retries the
same request**. A retry is safe by the idempotency property above: it only re-touches
whatever a prior partial run did not reach. There is no separate "resume" endpoint —
the retry IS the resume.

## The audit trail (`privacy_requests`)

One row per invocation, **surviving the erasure it records** — this is the whole point
of an LGPD audit trail: proof that a request was honored, without the row itself being
personal data.

| column | notes |
|---|---|
| `id` | UUID PK |
| `kind` | `"erasure"` \| `"export"` |
| `subject_type` | `"patient"` \| `"user"` |
| `subject_hash` | sha256 hex of the **normalized** subject key — see below |
| `requested_by` | the acting admin's `users.id`, `ON DELETE SET NULL` (the audit row outlives the admin account) |
| `status` | `"completed"` \| `"partial"` |
| `result` | JSON: **counts + per-service status only** |
| `created_at` | timestamp |

### The "identifiers only as sha256" rule

`subject_hash` is computed from a normalized key (`user:<email>` or
`patient:<tenant_id>:<wa_id>[:<email>]`, lower-cased) **before** any deletion, and is the
ONLY reference to the subject ever persisted in `privacy_requests`. The raw email/wa_id
is never written to this table, never logged (the erasure log line
`privacy_erasure_executed` carries `subject_hash`, not the raw identifier — structlog's
`redact_secrets` processor is defense-in-depth on top of this, not the primary
guarantee), and never appears in `result`.

`result` for an **erasure** already contains only counts (`{"erased": {...}}`) or a
status marker, so it is stored as-is. `result` for an **export** is different: the
upstream/local bundles contain actual field values (names, messages, clinical notes),
which must NEVER be persisted. `services/privacy.py::_summarize_counts` reduces each
bundle to `{key: <list length> | 1 | 0}` before it is written — the **response** to the
admin carries the full bundle (that is the point of an export), but the **audit row**
never does.
