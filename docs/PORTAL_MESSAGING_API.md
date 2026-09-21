# Portal Messaging API — the Brain-Message channel

> **Status:** integration reference, verified against code on **2026-09-18**. This is the
> ONE place that documents how a patient exchanges messages with a clinic through the
> Brain-Message channel (the web "Portal" `Brain-Message-Frontend`, as opposed to WhatsApp),
> and what a new product would need to build to appear on it. It does not replace
> `CONTRACTS.md` (brain-api's own auth/entitlement/billing/onboarding contract, which this
> file assumes) or any product's own `docs/CHECKPOINT_*.md` — those stay the source of truth
> for *why* a decision was made and for that product's internal implementation. This file is
> the source of truth for the **wire contract** between the four repos.
>
> Every claim below cites the file it was read from. If a citation and the running code
> disagree, the code wins — update this file in the same change that breaks it (see
> `AI_WORKFLOW.md`'s documentation rule: update docs after validating, not before).

---

## 0. Topology

There is no `Brain-Message-Backend` — that repository is git-empty on purpose (confirmed:
`git log` returns nothing) and stays that way. All logic lives in three existing services
plus the frontend that talks to them:

```
                    ┌─────────────────────────────────────────────┐
                    │                 brain-api                    │
patient (browser) ─►│  /patient-access/*  (identity + switchboard) │─┬─►  secretarIA
                    └─────────────────────────────────────────────┘ │    /internal/brain-message/*
                                                                     └─►  PreCheck
                                                                          /internal/brain-message/*

clinic staff ───────────────────────────────────────────────────────────►  secretarIA
  (Brain-Message-Frontend "Chat" module)                                   /tenants/me/conversations/*
                                                                             (hub-token, direct — never via brain-api)

clinic staff ───────────────────────────────────────────────────────────►  PreCheck
  (Brain-Message-Frontend "Anamneses" module)                              /precheck-sessions/*
                                                                             (SSO token, direct, READ-ONLY)
```

Two independent network paths, not one — this is the distinction most likely to trip up a
new integration:

- **Patient → brain-api → product.** brain-api owns the patient's identity (OTP-by-e-mail,
  session, multi-clinic account) and never speaks for a product — it only decides which
  product a message is for and relays it. Verified: `brain-api/src/brain_api/api/
  patient_access.py`, `brain-api/src/brain_api/services/message_switchboard.py`.
- **Staff → product, directly.** The clinic's console (`Brain-Message-Frontend`) calls
  secretarIA's hub and PreCheck's staff endpoints itself, with its own tokens. brain-api is
  **not** in this path. Verified: `Brain-Message-Frontend/lib/real/console-api.real.ts`
  (header comment lists `hubFetch`/`precheckFetch`, never `/api/brain/` for these calls).

A product joining this channel therefore has two integration surfaces, not one: the
patient-facing pair under `/internal/brain-message/*` (this document, §2–§4) and its own
staff-facing surface (already built per-product; secretarIA's is read/write, PreCheck's is
read-only — §3.2).

| Repo | Owns | Does NOT own |
|---|---|---|
| `brain-api` | Patient identity, session, multi-clinic account, LGPD consent to be contacted, the switchboard that picks a product | Any message content, any conversation storage |
| `secretarIA` | Its own conversation/message storage for the Brain-Message channel, exactly like it already does for WhatsApp | Patient identity (receives an opaque `external_id` from brain-api) |
| `PreCheck` | Its own `precheckv2.sessions`/`answers` storage, its own conductor | Patient identity (receives an opaque `session_ref`) |
| `Brain-Message-Frontend` | Two screens: `/conversa` (patient, talks only to brain-api) and the "Chat"/"Anamneses" modules (staff, talk directly to secretarIA/PreCheck) | Any backend logic |

**A note on `tenant_id` identity.** Every `tenant_id` above is brain-api's `tenants.id` (a
UUID). The two existing products map it two different ways — a new product must pick one
before writing any query that filters by it:

- **secretarIA shares the same UUID as its own primary key**: `Tenant.id` is
  `Mapped[uuid.UUID]` (`secretaria/src/secretaria/models/tenant.py`) — no separate mapping
  column, `WHERE tenants.id = :tenant_id` just works.
- **PreCheck keeps its own integer `Clinic.id`** and stores brain-api's UUID in a separate,
  nullable, unique column, `Clinic.brain_tenant_id` (`PreCheck/app/models/clinic.py` —
  `String(36)`, NULL until the clinic is linked at onboarding).

Sharing the UUID as your own PK is simpler if your schema allows it; a mapping column is the
right call when you already have an existing integer-keyed schema to fit into (PreCheck's
case).

---

## 1. Authentication

Three unrelated credential schemes exist on this channel. Do not mix them up — each one is
checked by a different piece of code and a token from one is rejected by all the others.

### 1.1 Patient ↔ brain-api

The patient never holds a credential a product recognizes. brain-api mints purpose-scoped
JWTs of its own (`auth-jwt-multitenant` skill), verified in
`brain-api/src/brain_api/api/portal/patient_access.py` (module docstring spells out the trust
boundary):

| Token | Scope claim | Grants | Since |
|---|---|---|---|
| Account token | `patient_account` | `POST /clinics` (join a clinic by invite), end the account. Opens no thread. | 2026-09-15 |
| Clinic token | `patient_message`, one tenant | The everyday credential of the thread routes (§2–§4), one per clinic the account joined | — |
| Pending token | `patient_pending` | The thread routes of exactly one clinic, for a visitor who has proven nothing yet (no e-mail confirmed) | 2026-09-16 |

All three are mutually exclusive by decoder: `decode_patient_token`/
`decode_patient_account_token`/`decode_patient_pending_token` each accept only their own
`scope`, so a pending token can never reach an account route and vice versa. The revocable
leg is the `__Host-patient_session` (account/clinic) or `__Host-patient_pending` (pending)
HttpOnly cookie; the short-lived JWT lives in the browser's memory only, never in
`localStorage` (enforced by a test reading this file's source —
`Brain-Message-Frontend/lib/real/patient-access.ts`, header comment).

Login is e-mail + one-time code (`POST /patient-access/request-otp` /
`/verify-otp`), never a phone number — there is no WhatsApp identity on this channel. A
clinic joins the account only by invite (its link, its short code, or a pasted UUID) — never
by e-mail match. Full state machine: `docs/CHECKPOINT_portal_clinicas_convite.md`,
`docs/CHECKPOINT_portal_sessao_pendente.md`; the account-linking security argument is the
skill `cross-tenant-account-linking`.

### 1.2 Staff ↔ product (unaffected by this channel, listed for completeness)

A clinic staff member never goes through brain-api's switchboard. secretarIA gates its hub
with an introspected hub token (`CONTRACTS.md` §12.2); PreCheck is reached with an SSO token
brain-api mints on request (`POST /sso/precheck/token`,
`brain-api/src/brain_api/api/sso.py`, `CONTRACTS.md` §10). Neither of those tokens is valid
on the `/internal/brain-message/*` routes below — they authenticate a human, not a service.

### 1.3 brain-api ↔ product — the two internal keys

This is the hop a new product actually has to implement. Both keys are **symmetric,
service-pair secrets**; neither ever reaches a browser. Verified:
`brain-api/src/brain_api/services/message_switchboard.py` (module docstring: "TWO KEYS, TWO
HEADERS, AND THEY ARE NOT INTERCHANGEABLE").

| Product | Header brain-api sends | Verified by | Missing header | Wrong value | Server key unset |
|---|---|---|---|---|---|
| secretarIA | `X-Internal-Api-Key` | `secretaria/src/secretaria/api/internal.py::require_internal_api_key` | 401 `Invalid internal API key.` (header defaults to absent, same code path as "wrong") | 401 `Invalid internal API key.` | **403** `Internal API not configured.` |
| PreCheck | `X-Internal-Token` | `PreCheck/app/core/deps.py::require_internal_api_token` | **422** (FastAPI's own validation — the header is a required parameter, so the dependency body never runs) | 401 `Token interno invalido` | **503** `Internal API nao configurada` |

Two real asymmetries a new product should decide on purpose, not by accident: whether the
header is FastAPI-required (→ 422 on a missing header, PreCheck's choice) or optional-then-
checked (→ 401, secretarIA's choice), and what an unconfigured server answers (secretarIA
treats "no key configured" as *the surface is locked* — 403; PreCheck treats it as *try again
later* — 503). Both compare in constant time (`secrets.compare_digest`), and secretarIA
additionally accepts an `INTERNAL_API_KEY_PREVIOUS` value for rotation-without-downtime —
confirm the PreCheck equivalent before assuming parity (not verified in this pass).

brain-api's own outbound side (`message_switchboard.py::_upstream`) has a third, distinct
failure: if **brain-api's own** copy of a product's base URL or key is unset, it never makes
the call and answers the *patient* with **503 `product_channel_unconfigured`** — this is an
operator misconfiguration on brain-api's side, not a statement about whether the sibling
service is healthy.

### 1.4 The pending-visit reverse call (aside)

One call runs in the opposite direction: secretarIA calls brain-api at
`POST /internal/brain-message/pending-email` to record the e-mail a visitor typed **in the
chat**, before any code is sent (`brain-api/src/brain_api/api/portal/internal.py::
claim_pending_email`, `schemas/portal/internal.py::PendingEmailClaimIn`). This exists solely so a
browser can never name the inbox a login code goes to — the address must have been captured
server-to-server, from the conversation itself. A new product does not need to implement this
to appear on the channel (§9); it is part of the *pending visitor* flow, which is optional.

---

## 2. Send — patient → clinic

**`POST /patient-access/threads/{product}/messages`** (brain-api,
`api/portal/patient_access.py::send_thread_message`). `product` is `"secretaria"` or `"precheck"` —
chosen by the client (the tab the patient is on), never inferred from text. Authorization:
a clinic token or a pending token (`get_thread_patient`), for the tenant that token names —
there is no field in the body that could point at another clinic.

Request body (`schemas/portal/patient_access.py::PatientMessageIn`, `extra="forbid"`):

```jsonc
{
  "text": "Gostaria de remarcar minha consulta para sexta-feira",
  "patient_name": "Ana Souza",          // optional, ≤200 chars
  "interactive_reply_id": null          // optional, ≤256 chars — see §5
}
```

`text` is capped at 4000 chars (PreCheck's own ceiling, matched here so an over-long message
is a clear 422 at the door instead of an opaque failure upstream).

brain-api resolves the tenant's entitlement, refuses with **403 `product_unavailable`** if
the clinic does not have that product AND the Brain-Message channel both on
(`message_switchboard.py::require_product` — one answer for "no such product", "not bought"
and "channel off", so a session cannot enumerate what a clinic pays for), then relays via
`message_switchboard.py::send_message`. The two products' inbound contracts are **shaped
differently on purpose** — this is not an oversight to fix, it mirrors how differently they
actually process a turn:

### 2.1 → secretarIA (async, 202)

```
POST /internal/brain-message/inbound          X-Internal-Api-Key: <shared secret>
{
  "tenant_id": "5b6c7a10-...-uuid",
  "external_id": "b7e21fa0-...-patient-ref-uuid",   // MessagePatient.id, opaque handle
  "text": "Gostaria de remarcar minha consulta para sexta-feira",
  "patient_name": "Ana Souza",
  "interactive_reply_id": null,
  "dedupe_id": null                              // optional idempotency key, off by default
}
```

Response: **202** `{"status": "queued"}` immediately — the turn runs on the arq worker
(`process_brain_message_inbound`), never inline in the request
(`secretaria/src/secretaria/api/internal.py::brain_message_inbound`; the `whatsapp-webhook-
arq` golden rule applies here too, by design, per that function's own docstring). **503**
`{"detail": "Queue unavailable."}` if the arq pool is down — fail-closed, so the caller learns
the message was **not** accepted rather than silently dropped.

`dedupe_id` is optional idempotency: unlike a WhatsApp webhook, brain-api calls this once per
patient action over an authenticated request, so there is normally nothing to de-duplicate;
supplying it opts into the same `processed_events` claim the WhatsApp path uses
(`schemas/internal.py::BrainMessageInbound`).

### 2.2 → PreCheck (sync, 200)

```
POST /internal/brain-message/inbound          X-Internal-Token: <shared secret>
{
  "tenant_id": "5b6c7a10-...-uuid",
  "session_ref": "b7e21fa0-...-patient-ref-uuid",   // same opaque handle, different field name
  "text": "Sim",
  "patient_name": "Ana Souza"
}
```

Response: **200**, synchronously, with the *whole next turn* already computed —
`PreCheck/app/schemas/brain_message.py::BrainMessageInboundResponse`:

```jsonc
{
  "session_ref": "b7e21fa0-...",
  "session_id": "4821",
  "clinic_id": 12,
  "state": "ACTIVE",
  "status": "question",     // see §8.3 for the full enum
  "messages": ["Perfeito, seguimos com a próxima pergunta."],
  "question": { "index": 3, "question_id": "q_sintomas", "text": "Há quanto tempo sente isso?", "type": "text" }
}
```

This is why brain-api's client just returns each product's payload through an envelope
(`RelayOut`, `extra="allow"`) instead of a shared shape: freezing one would force a
synchronized deploy the moment either side adds a field
(`schemas/portal/patient_access.py::RelayOut` docstring cites `frozen-contract-migration`).

`interactive_reply_id` (§5) rides **only** on the secretarIA leg — PreCheck's inbound model is
`extra="forbid"` with no such field, and the switchboard drops it before calling PreCheck
rather than risk a stray-key 422 from a healthy service
(`message_switchboard.py::send_message` docstring).

---

## 3. Send — clinic → patient

This half is **not** brain-api's job — the clinic's console talks to each product directly
(§0). There is no single endpoint here; there are two, with very different capabilities.

### 3.1 secretarIA — read/write

**`POST /tenants/me/conversations/{id}/messages`**
(`secretaria/src/secretaria/api/hub/conversations.py::send_message`, hub-token auth). Body:
`{"body": "<text>"}`. The endpoint dispatches by `Patient.channel` through a
`ChannelSender` (`services/channel_sender.py`):

- **WhatsApp** (`channel != "brain_message"`): the tenant's `WhatsAppClient`, addressed to
  `wa_id`. Meta carrying the message *is* the delivery; the history row is written from the
  send response afterwards.
- **Brain-Message**: `BrainMessageSender(author=MessageSender.HUMAN)`, addressed to the
  conversation (no network leg, no phone number). Writing the row **is** the delivery — the
  patient's portal polls for it (§4) — so it is flushed into the SAME transaction as the
  handover flip and commits with it, or not at all.

Either way a human send always takes the conversation over
(`HandoverManager.set_human_active`), whichever channel carried it.

**Status as of 2026-09-18 — a known bug, now fixed (uncommitted).** Until
`z_prompts/PROMPT_BRAIN_MESSAGE_SECRETARIA_CONSOLE_SEND_CHANNEL_DISPATCH.md` ran (Onda 0 of
`PLANO_PORTAL_API_MVP.md`), this endpoint built a `WhatsAppClient` **unconditionally**. A
Brain-Message patient has `wa_id=None` by design, so the Graph API received `"to": null`,
Meta answered 400, and the console saw a bare **502** the first time it was reproduced live
(2026-09-09). A same-day patch (`5b8bfdf`) contained the symptom with an inline branch that
skipped the WhatsApp call but did **not** go through `ChannelSender` — `BrainMessageSender`
was hardcoded to `sender=BOT`, so a staff reply was recorded as if the bot had said it. The
fix above (2026-09-18, **BUILT, full suite green, still UNCOMMITTED in the secretarIA working
tree — not deployed**) routes through `_staff_sender` → `ChannelSender` for real, and
`BrainMessageSender` gained an `author` parameter so a staff reply now records
`sender=MessageSender.HUMAN` correctly. Verified directly by reading
`api/hub/conversations.py::_staff_sender`/`send_message` on 2026-09-18 — do not trust a
"not executed" framing from an older draft of this same integration; re-read the code before
repeating it. Full account: `secretarIA/docs/CHECKPOINT_console_staff_messages.md` §9.

### 3.2 PreCheck — read-only, by design

PreCheck's staff surface for this channel is **`GET /precheck-sessions`** (list) and
**`GET /precheck-sessions/{session_ref}/messages`** (transcript) only — there is no reply
endpoint, no take-over. `Brain-Message-Frontend/lib/real/console-api.real.ts::
readOnlyPrecheck` refuses every write action for a PreCheck thread with a 501 naming the
reason: "the PreCheck questionnaire is read-only in the console." This is confirmed as
**intentional design**, not a gap waiting on this document's plan — none of the four waves of
`PLANO_PORTAL_API_MVP.md` change it.

---

## 4. Receive — poll

Both directions on the patient's side are pull-based; there is no push channel (WebSocket/SSE)
in this round — recorded as a deliberate decision, not an oversight
(`message_switchboard.py::list_messages` docstring).

**`GET /patient-access/threads/{product}/messages?since=<cursor>`** (brain-api). `since` is
passed through **opaque** — brain-api does not know or reformat either product's cursor
format, because re-formatting a value it does not own is exactly how a cursor silently starts
skipping a row.

### 4.1 secretarIA

```
GET /internal/brain-message/conversations/{external_id}/messages?tenant_id=...&since=2026-09-18T14:00:00Z
```

Scoped by **all three** of `tenant_id` (required query param — an `external_id` alone is a
bearer-like handle, so making the tenant optional would turn a guessed id into a cross-tenant
read), `external_id` (path) and `channel == "brain_message"`
(`api/internal.py::list_brain_message_messages`). `since` is an exclusive timestamp cursor —
**since 2026-09-19 (Onda 3) on `updated_at`, not `created_at`**: a row returns again whenever its
delivery state moves, so the client upserts by `id` (§8.5). An unknown patient or an empty conversation returns
`{"data": []}`, **never 404** — this endpoint is polled, so "nothing yet" must not be
distinguishable from "wrong id" (`BrainMessageMessageList`):

```jsonc
{ "data": [
  { "id": "c1e9...", "direction": "outbound", "sender": "human",
    "body": "Sua consulta foi remarcada para sexta às 14h.",
    "created_at": "2026-09-18T14:33:10Z", "interactive": null, "interactive_reply_id": null }
] }
```

### 4.2 PreCheck

```
GET /internal/brain-message/sessions/{session_ref}/messages?tenant_id=...&since=...
```

Same non-enumerable-by-design answer: an unknown clinic or ref returns an empty transcript,
never 404 (`app/routers/internal.py::brain_message_messages`). `since` is an exclusive UTC
instant; event ids break ties within the same instant. Response shape
(`BrainMessageTranscriptResponse`) includes `history_gap` — true when the flow can no longer
prove which question a stored line belongs to (a flow was swapped, or a followup outlived its
parent), so the client knows to render defensively rather than guess.

---

## 5. Interactive messages (buttons & lists)

An outbound interactive control is stored as one JSON blob per message, in the SAME shape
secretarIA already uses for WhatsApp (`secretaria/src/secretaria/models/message.py`,
`Message.interactive`, lines 68–79 — no separate table):

```jsonc
{
  "kind": "buttons",                 // or "list"
  "body": "Você confirma a consulta de amanhã às 10h?",
  "options": [
    { "id": "confirm|9f21ab3e", "title": "Confirmar", "description": null },
    { "id": "cancel|9f21ab3e",  "title": "Cancelar",  "description": null }
  ],
  "button_label": null,              // "list" only
  "section_title": null              // "list" only
}
```

**Only the WhatsApp send path writes this column.** A Brain-Message patient receives the same
options flattened into plain text by `ChannelSender.interactive_history_body` — this is a
recorded design decision (`docs/CHECKPOINT_interactive_bubbles.md`), not a gap: the column
exists so the **staff console** can render the real controls the patient saw on WhatsApp, and
so a Brain-Message reply can still resolve against them (below).

A tap becomes `interactive_reply_id` on the **inbound** side — the id of the button/row, not
the label:

```jsonc
{ "text": "Confirmar", "interactive_reply_id": "confirm|9f21ab3e" }
```

This id is **untrusted** — unlike a WhatsApp tap it was never signed by Meta, it comes
straight from the patient's own browser through the switchboard. secretarIA's worker
(`_validated_brain_message_reply_id`) only honours it after finding it among the options its
own recent cards actually offered on that conversation; anything else is routed as plain text
(`schemas/internal.py::BrainMessageInbound` docstring). Bound at 256 chars — the longest id a
reply-button card can carry (`secretaria.core.whatsapp_limits.MAX_INTERACTIVE_REPLY_ID_CHARS`,
the same bound `brain-api/schemas/portal/patient_access.py::PatientMessageIn.interactive_reply_id`
enforces). As noted in §2.2, this field is **secretarIA-only** — PreCheck's questionnaire
takes the option's **label** as plain `text` instead, so a client sends the same tap either
way and the switchboard decides which shape to build.

PreCheck's own rendered-question shape is channel-neutral by design
(`PreCheck/app/schemas/brain_message.py::RenderedQuestion` — `type`/`options`/`list_options`),
**not** a WhatsApp payload: n8n folds the Meta interactive shape for its channel; Brain-Message
renders its own controls from the same flow data.

---

## 6. Text formatting

`*bold*`, `_italic_`, `~strike~`, `` `code` ``, ` ```mono``` ` (mono may span lines) — the
same inline syntax WhatsApp itself draws. This is a **client-side rendering convention only**:
neither brain-api nor any product interprets, strips or transforms these markers. The text
column always holds the raw marker string; only `Brain-Message-Frontend/lib/whatsapp-
format.ts` parses it into a tree (`FormatNode[]`) and renders `<strong>/<em>/<s>/<code>` —
never `dangerouslySetInnerHTML`, since a patient's text is untrusted input. A marker opens
only when not glued to a word on its left and closes only when not glued on its right (so
`2*3*4`, `ana_maria@x.com` and `* não *` all stay literal), never crosses a line break, and
nests freely outside of `code`/`mono`. A new product needs to do **nothing** to support this —
it is purely how the existing frontend draws whatever text arrives.

---

## 7. Attachments / media — built end to end (2026-09-18; all three parts uncommitted, not deployed together)

**State:** the brain-api half (part 1 of `z_prompts/PLANO_PORTAL_API_MVP.md` Onda 2) is BUILT and
tested against a wire-level fake of secretarIA — **not committed, not deployed, and inert until
secretarIA's half (`..._2_SECRETARIA.md`) and the frontend (`..._3_FRONTEND.md`) exist.** The secretarIA half is §7.5 and the frontend half §7.6 (both built the same day, both uncommitted);
the chain has NOT yet been proven in a browser with a real file in both directions. Full contract,
decisions and proofs:
`docs/CHECKPOINT_brain_message_anexos.md`. The limits live in ONE place,
`src/brain_api/core/attachments.py` — copy the numbers from there, never re-derive them.

### 7.1 Patient → brain-api: send a file

`POST /patient-access/threads/{product}/messages` — the SAME route as §2, now in two encodings:
`application/json` sends text exactly as before; `multipart/form-data` sends ONE part `file`
(JPEG/PNG/WEBP/GIF/PDF **judged by its bytes**, 1 to 20 971 520 bytes) plus the JSON fields as form
fields, `text` becoming an optional caption. Any other field is a 422. Any patient may upload —
clinic token or pending (e-mail not verified) token alike — but only on a product in
`ATTACHMENT_PRODUCTS` (today `secretaria`; PreCheck → 422). The type relayed is the sniffed one — never the browser's `Content-Type` or the
extension; a same-family wrong extension is corrected, a cross-family one refused; names are
sanitized. Budgets: `PATIENT_ATTACHMENT_RATE_LIMIT_PER_MIN` (10/patient) and, for unverified
visitors, `PATIENT_PENDING_ATTACHMENT_RATE_LIMIT_PER_MIN` (20/clinic, shared); kill switch
`PATIENT_ATTACHMENTS_ENABLED`. Success: `200` + `RelayOut`.

### 7.2 brain-api → secretarIA (what part 2 must accept)

`POST /internal/brain-message/inbound` — same path and key as §2.1, `multipart/form-data`:
`tenant_id` + `external_id` (from the session), optional `text`/`patient_name`/
`interactive_reply_id`, and one part `file` (sanitized name, sniffed `Content-Type`). secretarIA
must accept both encodings on that path, re-validate with the same constants, and accept whatever
brain-api accepted — a refusal after brain-api's check surfaces as `502 product_error` (contract
drift), except the two refusals only secretarIA can make (§7.4, lower table). Timeout `ATTACHMENT_UPSTREAM_TIMEOUT_SECONDS` (60 s per network operation). New:
`GET /internal/brain-message/media/{message_id}?tenant_id=&external_id=` → raw bytes, or one `404`
for "missing / not this conversation / no attachment" — looked up in ONE query by
`(message id, tenant_id, external_id)`: this route, not brain-api, is what keeps patient B out of
patient A's file. Transcript items gain `attachment: {content_type, size_bytes, filename} | null`,
and no field anywhere in the transcript carries a storage key or URL (brain-api rewrites only
`attachment`). secretarIA must also enforce a **persisted byte quota** per patient and per clinic
where it stores files — brain-api's limit is a best-effort count per process.

### 7.3 Receive: the reference in the poll, the bytes in the media route

`GET .../messages` rewrites each `attachment` into exactly
`{content_type, size_bytes, filename, media_path}` (a whitelist: object keys and signed URLs are
dropped; a malformed reference becomes `null`). `media_path` =
`/patient-access/threads/{product}/media/{message_id}` (relative to brain-api), which streams the
bytes under the thread's bearer (clinic or pending token) with `nosniff`, a sandboxing CSP,
`Cache-Control: private, no-store` and CORP same-origin; an upstream type outside the five is a
502, never streamed. Another patient asking for the same id gets the same `404` as a
nonexistent one. Because it needs the `Authorization` header, the portal must `fetch` it and render
the bytes — with its CSP (`img-src 'self' data:`) that means a `data:` URL or adding `blob:`.

### 7.4 Refusals — 4xx, never 5xx (permanent: resending the same file never passes)

Body `{"detail": {"code", "message"}}` (+ `max_bytes` on 413); `message` is Portuguese, shown as is.

| `code` | Status | When |
|---|---|---|
| `attachment_too_large` | 413 | file over 20 971 520 bytes, or the body over its cap (declared or streamed) |
| `attachment_type_unsupported` | 415 | content is none of the five kinds (HTML, SVG, BMP, ...) |
| `attachment_type_mismatch` | 422 | the name claims another family, or an extension outside the list |
| `attachment_empty` | 422 | 0 bytes |
| `attachment_malformed` | 422 | no `file`, two files, a repeated field, broken multipart |
| `attachment_unsupported_for_product` | 422 | product takes no files (PreCheck), or kill switch off |
| `attachment_not_found` | 404 | media route, every reason |

Decided by the **product** after brain-api accepted the file (since 2026-09-19 relayed as these
4xx — before, both surfaced as `502 product_error`, which EasyPanel's gateway swaps for its own
"Service is not reachable" HTML page; see `docs/CHECKPOINT_brain_message_anexos.md` §10). Same
body shape, brain-api's own `message` — the product's body still never reaches the browser, and
only when the product's status agrees with this table (`core/attachments.py::PRODUCT_REFUSALS`):

| `code` | Status | When |
|---|---|---|
| `attachment_consent_required` | 409 | the patient has not accepted the LGPD terms in this conversation yet — a new visitor's first message is text, then "Concordo", then a file |
| `attachment_quota_exceeded` | 429 | secretarIA's persisted daily byte quota (per patient or per clinic) — distinct from the per-minute `429 "Too many requests"` (string `detail`) |

Storage unavailable stays the retryable `503 product_temporarily_unavailable` (§8.1).

Decisions already closed for the whole chain (do not re-litigate them; the prompts have the full
argument):

- Ships for **secretarIA first**, PreCheck explicitly deferred (its own paridade series,
  `PROMPT_BRAIN_MESSAGE_PRECHECK_PARIDADE_*.md`, is a separate, larger, already-deferred effort
  that touches production WhatsApp code and needs its own authorization).
- Scope is **transport, not clinical intelligence** — the API delivers the file; it does not
  OCR or summarize it. Reading a medical image is a PreCheck product feature, not a channel
  capability.
- secretarIA gets its **own** R2 bucket/credentials — it does not call PreCheck's storage.
- **Multipart end-to-end** (browser → brain-api → secretarIA), never base64 (avoids ~33%
  inflation and double-buffering the whole file in memory).
- **One JSON column** on `messages` (`attachment`), mirroring the `interactive` precedent —
  not a new table.
- Limit: **20 MB**, image (`jpeg`/`png`/`webp`/`gif`) + PDF — reusing the number the earlier,
  never-executed PreCheck attempt already settled on.

**When this ships, update THIS section in place — do not create a new document.** That is how
this file is meant to grow (see the changelog at the bottom).

---

### 7.5 secretarIA side (part 2) — built 2026-09-18, uncommitted, not deployed

Implements §7.2 as specified (`secretarIA/docs/CHECKPOINT_brain_message_anexos_secretaria.md`):
multipart on the same inbound path, re-validated with the same numbers; the API stores the
file in secretarIA's own R2 bucket and the worker job carries only a reference; the bot
answers with a fixed receipt, never an analysis. The poll's `attachment` carries only
`{content_type, size_bytes, filename}`; `GET /internal/brain-message/media/{id}` streams the
bytes, ownership checked in one query. Three refusals exist only on that side; brain-api passes
the two permanent ones through since 2026-09-19 (§7.4, lower table) and keeps the 503 as its
own `product_temporarily_unavailable`:

| `code` | HTTP | When |
|---|---|---|
| `attachment_consent_required` | 409 | the patient has not accepted the LGPD terms (or is unknown) |
| `attachment_quota_exceeded` | 429 | persisted rolling-24h byte quota: per patient (100 MiB) or per clinic (1 GiB) |
| `attachment_storage_unavailable` | 503 | storage unconfigured or down — retryable |

A captionless file's `body` is `"[anexo: <filename>]"`; a screen that draws the file may omit
exactly that string. Staff (direct to secretarIA): `POST /tenants/me/conversations/{id}/messages`
as multipart (Brain-Message patients only) and `GET /tenants/me/conversations/{id}/messages/{mid}/media`.

### 7.6 Frontend (part 3) — built 2026-09-18, uncommitted, not deployed

`Brain-Message-Frontend/docs/CHECKPOINT_brain_message_anexos_frontend.md`. Both surfaces (`/chat` staff,
`/conversa` patient) send ONE file per message as multipart through `XMLHttpRequest`
(`lib/real/upload.ts`), so the bubble's progress bar is the real upload percentage; the text is the
caption; nothing is validated on the client beyond the picker's `accept` list — the card shows the
backend's `detail.message` as it is, with retry of the same file and cancel. `content_type` maps to
the card kind (`image/*` → picture, `application/pdf` → PDF, else generic file). The transcript's
`attachment` becomes `Attachment.url` = `/api/brain` + `media_path` (patient) or the hub's
`.../messages/{id}/media` (staff); the bytes are fetched under the session and shown from an object
URL used only inside `<img>` and `<a download>` (nginx CSP gained `blob:` in `img-src`; `client_max_body_size
25m` and `proxy_read_timeout 90s` on the two attachment hops). A captionless file's placeholder body
is omitted. Closed 2026-09-19 on the brain-api side: secretarIA's 409/429 now reach the card as
`{code, message}` (before, a `502 product_error` that EasyPanel turned into its HTML page). Proven live on
2026-09-18 (patient side, `next dev` against production): the multipart reaches brain-api, a bad file is
refused with the real 415 message on the card, and a good file gets `503 product_temporarily_unavailable`
because production secretarIA answered 503 to the inbound multipart (storage most likely unconfigured) —
brain-api's own string replaces secretarIA's `{code, message}` on that hop. The staff side, tested the same
night straight against the hub, got secretarIA's `503 attachment_storage_unavailable` with its own message on
the card — so the blocker is secretarIA's storage configuration, not the transport.

## 8. Errors and status codes

Three different hops, three different vocabularies. None of this was written down in one
place before this document.

### 8.1 Patient ↔ brain-api (`/patient-access/threads/*`)

| Status | Meaning | Notes |
|---|---|---|
| 200 | Relayed successfully | Body is `RelayOut` — the product's own payload, passed through |
| 401 | Missing/invalid/expired patient session | Clinic or pending token |
| 403 | `product_unavailable` | Clinic does not have that product, or the Brain-Message channel is off — **one answer for both**, so a session cannot enumerate what a clinic pays for |
| 422 | Malformed body | `extra="forbid"`, field length/shape violations (FastAPI validation) |
| 429 | Rate limited | Per-IP/per-address/per-account limiters, route-dependent (`patient_access.py` header comments name each budget); uploads have their own per-patient budget |
| 409 / 413 / 415 / 429 (+ some 404/422) | Attachment refusals | `{"detail": {"code", "message"}}` — full table in §7.4 |
| 502 | `product_unreachable` / `product_error` | The product's leg is down, or answered something brain-api could not parse/trust. **Never** the same code as 403 — a missing tab and a real outage must read differently to a support call. **In production the browser never sees this JSON:** EasyPanel's gateway replaces any 502 the app sends with its own HTML "Service is not reachable" page (observed 2026-09-18/19; a 503 passes through intact) — a client must treat a non-JSON 502 as this row, and an operator must read brain-api's log (`switchboard_upstream_error status=...`) before assuming a crash |
| 503 | `product_channel_unconfigured` / `product_temporarily_unavailable` | brain-api itself has no base URL/key for that product (operator fact), or the product answered its **own** 503 (passed through, because PreCheck's `stage_unsupported_on_channel`/`clinic_flow_not_configured` are real "not available here" facts worth showing as such) |

### 8.2 brain-api ↔ secretarIA (`/internal/brain-message/*`)

| Status | Meaning |
|---|---|
| 202 | Accepted, queued on the arq worker |
| 401 | `Invalid internal API key.` — missing or wrong `X-Internal-Api-Key` |
| 403 | `Internal API not configured.` — no server-side key at all; the surface is locked |
| 503 | `Queue unavailable.` — arq pool down; the message was **not** accepted (fail-closed, never silently dropped) |

### 8.3 brain-api ↔ PreCheck (`/internal/brain-message/*`)

| HTTP status | Meaning |
|---|---|
| 200 | Turn computed — see the **in-band** `status` field below |
| 401 | `Token interno invalido` — wrong `X-Internal-Token` |
| 404 | `no_clinic_for_brain_tenant` (pre-flight) or `flow_not_found` (inside the conductor) — two different checks, same code |
| 422 | Missing `X-Internal-Token` header at all (FastAPI required-header validation) |
| 500 | `conductor_failure` — an unmapped defect, logged with frame locations only (never bound SQL parameters, which can carry clinical text) |
| 503 | `clinic_flow_not_configured` (pre-flight) or `agent_unavailable` (mid-turn classification/state failure) |

The **200** response body also carries its own status, which is not an HTTP code and must not
be treated as one (`BrainMessageInboundResponse.status`, `PreCheck/app/schemas/
brain_message.py`):

| In-band `status` | Meaning | Client should |
|---|---|---|
| `awaiting_consent` | LGPD gate open, nothing recorded yet | Render the consent prompt |
| `question` | A question is on the table | Render `question` |
| `completed` | Questionnaire finished | Render the closing messages |
| `reset` | `/menu` wiped the session | Start over |
| `unsupported_on_channel` | The flow hit a deepening/media step that exists only on WhatsApp (n8n) | Render `UNSUPPORTED_STAGE_TEXT` as a **terminal** state for this session — this is a permanent refusal, not a transient one; retrying the same message will not help (see the skill `brain-mesh-permanent-vs-transient-refusal`) |

### 8.4 Cross-cutting rule

An unknown patient/session on any `GET .../messages` route always answers with an **empty**
list/transcript, never 404 — every one of these endpoints is polled, and a 404 would let a
caller enumerate which ids exist.

### 8.5 Message delivery state (enviado / entregue / lido / falhou) — secretarIA + brain-api

Added 2026-09-19 by the Onda 3 part 1 (secretarIA, committed `9cc9b7a`, **in production**: the
public `GET /build` reports `alembic_head 8b4d2f6e1a37` on both services, `deploy_parity: match`),
part 2 (brain-api, committed `7796949`, the read route answers 401 without a token in production,
so it is deployed) and part 3 (Brain-Message-Frontend, **committed `d56bd8f`, deployed and PROVEN
in production the same day** — `docs/CHECKPOINT_brain_message_status_entrega_frontend.md` §5.4
there: staff↔patient real accounts on the "Chrysostomo For Eyes" tenant, ticks progressing
`entregue → lido` both directions, one read call per cursor advance (~2 per side over ~25 polls),
12 interactive bubbles at 340px; only the WhatsApp-thread read-mark exclusion stayed proven
against the stub, not production, since that tenant has no WhatsApp patient). Status is mapped
1:1, read marks fire once per cursor move on both screens, never on a WhatsApp thread, never from
a hidden tab; neither client uses a `since` cursor — both re-read the whole thread, which is the
upsert by `id`. **Same day, still uncommitted:** the owner asked for two follow-ups after the
production proof — the read tick's green got a contrast bump (`--tick-read`) and the interactive-
bubble width dropped the `min(…, 100%)` percentage entirely for a fixed `340px` (a stale, never-
reloaded tab had made the old rule look broken on the owner's device) — same CHECKPOINT §5.5. Full
contracts: `secretarIA/docs/CHECKPOINT_brain_message_status_entrega.md` §4 (product side) and
`brain-api/docs/CHECKPOINT_brain_message_status_entrega.md` (patient side).

**Patient ↔ brain-api (part 2):**

- `GET /patient-access/threads/secretaria/messages` passes `status`, `delivered_at`, `read_at`
  and `updated_at` through **exactly as secretarIA sent them** — `RelayOut` is untyped per
  message, so nothing here re-derives or validates the state. `since` stays opaque and brain-api
  keeps no cursor of its own: it never filters, dedupes or re-sorts rows. The client upserts by
  `id` and takes the next cursor from the largest `updated_at`.
- **`POST /patient-access/threads/{product}/messages/read`**, clinic or pending token. Body:
  exactly one of `{"up_to_message_id": "<uuid>"}` or `{"up_to": "<ISO 8601 with offset>"}` —
  the same names as secretarIA's models, `extra="forbid"`. A body naming `tenant_id`,
  `external_id` or anything else is a **422** and nothing is relayed; so is a missing, double or
  naive cursor (checked here so it never becomes an upstream 422 → `product_error` 502).
  `tenant_id` + `external_id` upstream are the SESSION's. Answers `RelayOut` whose `payload` is
  secretarIA's `{"marked", "applied"}`. On `precheck` it answers `{"marked": 0, "applied": false}`
  with no network call, so the portal may mark every thread it shows. No limiter of its own
  (the same as sending a text message). 401 / 403 / 502 / 503 as the rest of §8.1.

**Product side (part 1):**

- Every secretarIA message now carries `status` (derived, never stored: `falhou` > `lido` >
  `entregue` > `enviado`), `delivered_at`, `read_at`, `updated_at` (console also `failure_reason`).
  `enviando` stays a client-only state.
- **`since` on `GET /internal/brain-message/conversations/{external_id}/messages` now compares
  `updated_at`, not `created_at`**: a message comes back when its status changes. Consumers must
  **upsert by `id`**, and the next cursor is the largest `updated_at` received.
- Brain-Message rows are delivered on persistence. `lido` needs a read mark:
  `POST /internal/brain-message/messages/read` (patient side, `X-Internal-Api-Key`, body
  `tenant_id` + `external_id` + exactly one of `up_to_message_id` / offset-aware `up_to`, answers
  `{"marked", "applied"}`; 422 for a missing, double or naive cursor) and
  `POST /tenants/me/conversations/{id}/messages/read` (staff side, hub token).
- WhatsApp status comes only from Meta's receipts. A read mark for a WhatsApp patient returns
  `applied: false` and changes nothing.
- PreCheck: not covered.

### 8.6 The automation speaks first — `POST {SECRETARIA}/internal/brain-message/open`

Added 2026-09-19 (TASK-003 §2; brain-api side **BUILT on `task/TASK-003-brain-api`, uncommitted
to `main`, not deployed**). Until this, a patient who opened a clinic's link landed in an
**empty** conversation: secretarIA's `Conversation` is born only from an inbound message, and a
patient who has not typed has not sent one. The owner asked for the automation to greet first.

```
POST {SECRETARIA_BASE_URL}/internal/brain-message/open     X-Internal-Api-Key: <pair key>
{ "tenant_id": "<uuid>", "external_id": "<MessagePatient.id>", "patient_name": null }
→ 202 {"status": "queued"}    conversation created, greeting enqueued
→ 200 {"status": "exists"}    a conversation with a message already existed — NOTHING sent
```

**brain-api calls it from two places, both fire-and-forget**
(`api/portal/patient_access.py::_greet_if_secretaria`, `services/message_switchboard.py::
open_conversation`):

| trigger | when | why |
|---|---|---|
| `POST /patient-access/pending` | only when the visit is **created**, never on a cookie resume | a reload must not greet twice |
| `POST /patient-access/clinics` | on every successful add | the owner's "paciente novo **da clínica**" already has a Portal account and never passes through `/pending` |

Both are gated on the **resolved product being secretarIA** — the link's `product`, or
`default_product(ent)` (the first offered tab) when the link names none.

- **No inbound message is ever fabricated.** That is the entire reason this route exists rather
  than relaying a synthetic "oi": a bubble the patient did not write must not appear in their
  own transcript.
- **Idempotence belongs to secretarIA**, not here. brain-api keeps no state about whether a
  greeting is due, which is what makes two trigger points safe.
- **It cannot fail or slow the patient's route.** It runs as a Starlette background task, after
  the response body is sent, and `open_conversation` never raises: an unconfigured mesh, a
  network error, a `404` from a secretarIA that does not have the route yet, and a `500` all
  collapse to a log line. **Deploy order is therefore free** — this service may go live before
  secretarIA's side, and the only consequence is that no greeting happens yet.
- **PreCheck now has one too** (TASK-004, 2026-09-20): the post-booking hand-off for a Portal
  patient no longer answers `501`. It calls PreCheck's `POST /internal/brain-message/open`, which
  opens the session with the welcome + LGPD gate and writes no patient-authored line. The
  `/inbound` route with `text: null` was considered and REJECTED: it is only safe in `INIT`, and a
  patient who opened the pre-consult tab before booking is already past it, where the same body
  would reach `_turn()` and could record an answer nobody gave. Contract and deploy order in
  `CONTRACTS.md` §12.3.2 (PreCheck first).

### 8.7 `email_masked` on `POST /internal/brain-message/pending-otp/request`

Added 2026-09-19 (TASK-003 §3, same rollout as §8.6). The response is now
`{"status": "sent", "email_masked": "a***a@gmail.com"}` — first character of the local part,
`***`, last character, `@`, and the **whole** domain (`core/email_mask.py`; a one-character
local part becomes `a***@dominio`).

secretarIA needs it because the code notice must say which inbox the code went to, and
secretarIA holds no copy of the address: the claim travels on the service leg
(`POST /internal/brain-message/pending-email`) precisely so the browser never names an inbox.
**The full address still never leaves brain-api** — not in this response, not in a log line
(`tests/test_patient_pending_session.py` pins both). `409 pending_email_missing` is unchanged,
and the field is absent from it: a visit with no captured address never reaches a `200`.
Additive — a consumer that ignores the field is unaffected.

---

## 9. How to adapt a new product to this channel

This is the checklist the rest of the document exists to support: **secretarIA and PreCheck
already prove the contract is per-product, not a shared library** — a third product plugs into
the same seam, not a new one.

### 9.1 What the new product's own service must build

1. **Two endpoints**, gated by a shared secret sent as a header from brain-api (pick your own
   header name and unconfigured/missing-header behavior — see §1.3 for the two existing,
   deliberately different, answers):
   - `POST .../brain-message/inbound` — accept one inbound patient message. Synchronous
     (PreCheck's model, full turn back) and fire-and-forget-then-poll (secretarIA's model,
     `202` + a worker) are **both** proven to work; pick whichever matches how your product
     already processes a turn. Do not invent a third shape without a reason.
   - `GET .../brain-message/.../messages` (or `.../sessions/{ref}/messages`) — poll the
     transcript, scoped by **both** `tenant_id` and the patient handle brain-api hands you
     (`external_id`/`session_ref` — an opaque string, never a phone number). Unknown handle →
     empty list, never 404 (§8.4).
2. **A boolean field on `ProductsOut`** (`brain-api/src/brain_api/schemas/entitlement.py`),
   backed by a new `entitlements.<product>_enabled` column — the same pattern `precheck`/
   `secretaria` already follow. The **channel** gate (`ChannelsOut.brain_message`, backed by
   `tenants.brain_message_enabled`) already exists and applies to every product automatically
   — you do not need a channel flag of your own, only the product flag. Once both are set for a
   clinic, your product starts appearing in `GET /patient-access/threads` and
   `POST /patient-access/clinics/lookup` for free — both call the same
   `message_switchboard.py::available_products()` this document's §2 uses, so there is nothing
   separate to register for discovery.
3. **A decision on `interactive_reply_id`** (§5): honour it (validate against your own recent
   "cards" before trusting it, like secretarIA does) or ignore it and read `text` only (like
   PreCheck does). Either is a legitimate, already-proven choice.

### 9.2 The small, mechanical addition brain-api's maintainer must also make

Contrary to a fully generic/config-driven dispatcher, `message_switchboard.py` hardcodes one
branch per product today (`PRODUCT_SECRETARIA`/`PRODUCT_PRECHECK`) — this is intentional
(the two contracts really do differ in shape, §2), but it does mean a third product is not
*purely* the new service's own work:

- Add a `PRODUCT_<NAME>` constant and an entry in the `PRODUCTS` tuple (controls tab order).
- Add `<NAME>_BASE_URL` / `<NAME>_...KEY` settings and a branch in `_upstream()`.
- Add one branch each to `send_message()` and `list_messages()` shaping the body/path the way
  your new product's two endpoints (§9.1) expect.

This is a handful of lines mirroring an existing branch, not a redesign — but it is real,
reviewable brain-api code, and should not be described to a new integrator as "zero backend
changes outside your own repo."

### 9.3 What the new product does NOT need to build

Everything brain-api already owns for every product on this channel: OTP/e-mail login,
session/token minting, the multi-clinic account model, LGPD consent to be contacted over this
channel, and the invite mechanism that adds a clinic to a patient's account. A new product
receives an already-authenticated patient and an opaque per-tenant handle — it never sees an
e-mail address, a cookie, or a JWT from this channel.

---

## 10. What this is NOT

- **Not the real WhatsApp Cloud API.** The WhatsApp channel itself is untouched by anything in
  this document — it continues to be `secretaria/src/secretaria/services/whatsapp.py` and
  PreCheck's n8n workflows. Brain-Message is a **second, independent** channel, not a
  reimplementation or a replacement of the first.
- **Not a public/external webhook surface.** Every route in §2–§4 lives inside the private
  `/internal/*` mesh, authenticated by a shared secret between exactly two services in this
  workspace. If a product **outside** this company ever needs to integrate, that is a new
  security surface (signed webhooks, external-facing auth, rate limiting against strangers)
  and deserves its own design and its own prompt — not a trivial extension of this file.
- **Not a substitute for each product's own `docs/CHECKPOINT_*.md`.** Those still hold the
  historical *why* behind a decision; this file holds only the current wire contract.

---

## Changelog

- **2026-09-18** — `PROMPT_BRAIN_MESSAGE_PORTAL_API_REFERENCE_DOC.md` (Onda 1 of
  `PLANO_PORTAL_API_MVP.md`). Document created: all ten sections above, each claim verified
  against the running code on this date. §7 (attachments) and the delivery-receipt note in §10
  are placeholders by design — the next wave to touch either capability must edit this file in
  place rather than create a new one.
- **2026-09-18** — `PROMPT_BRAIN_MESSAGE_ANEXOS_SECRETARIA_1_BRAIN_API.md` (Onda 2, part 1): §7
  filled in with the brain-api edge (built, not deployed) and the contract secretarIA's part 2 must
  meet; §8.1 points at the attachment refusals. Parts 2 and 3 complete §7 in place.
- **2026-09-18** — `PROMPT_BRAIN_MESSAGE_ANEXOS_SECRETARIA_3_FRONTEND.md` (Onda 2, part 3): §7.6
  added — the frontend half (both surfaces, multipart via XMLHttpRequest with real progress,
  authenticated media shown from object URLs); §7's heading and state updated to "built end to
  end, uncommitted, browser proof pending".
- **2026-09-18** — owner's decision: any patient uploads, e-mail verified or not (the pending
  token's 403 is gone); unverified uploads share a per-clinic budget. §7.1 and §7.4 updated.
- **2026-09-19** — `PROMPT_BRAIN_MESSAGE_ANEXOS_BRAIN_API_UPLOAD_CRASH.md`: the "upload crashes
  brain-api" 502 was secretarIA's `409 attachment_consent_required` flattened to `502
  product_error` and masked by EasyPanel's gateway (no crash — logs of both services). §7.4 gains
  the product-decided refusals (409 consent, 429 quota), relayed as 4xx; §7.2/§7.5/§7.6 and §8.1
  (EasyPanel masks every app 502) updated.
- **2026-09-19** — `PROMPT_BRAIN_MESSAGE_STATUS_ENTREGA_2_BRAIN_API.md` (Onda 3, part 2): §8.5
  gains the patient side — the poll relays the delivery state untouched (no brain-api cursor), and
  `POST /patient-access/threads/{product}/messages/read` relays the patient's read mark scoped by
  the session. §4.1 notes the `updated_at` cursor. Built, uncommitted, not deployed.
- **2026-09-19** — `BRAIN/tasks/TASK-003` (brain-api leg): **§8.6** — the automation speaks first.
  brain-api calls `POST {SECRETARIA}/internal/brain-message/open` fire-and-forget from two
  triggers, so a patient who opens a clinic's link no longer lands in an empty conversation; no
  inbound message is ever fabricated, and PreCheck gets no equivalent (`CONTRACTS.md` §12.3.2
  says why). **§8.7** — `email_masked` on the internal OTP-request response, so the code notice
  can name the inbox without the raw address leaving brain-api. Built on
  `task/TASK-003-brain-api`, not merged, not deployed; deploy order is free in both directions.
- **2026-09-19** — `PROMPT_BRAIN_MESSAGE_STATUS_ENTREGA_3_FRONTEND.md` (Onda 3, part 3): §8.5
  closed — Brain-Message-Frontend now drives `DeliveryTicks` and the read mark from the real
  state on both screens; committed `d56bd8f`, deployed and proven live the same day (staff↔patient
  real accounts). §10 drops the "not yet a delivery/read-receipt system" bullet — it shipped. A
  same-day owner follow-up (read-tick contrast, fixed interactive-bubble width) is noted in §8.5 as
  built but still uncommitted — update this section again once that lands.
- **2026-09-20** — TASK-004 shipped the hand-off §8.6 used to describe as missing, and took a
  bubble OUT of the Portal. `POST /internal/brain-message/open` (PreCheck) is the new leg; the
  `501 precheck_portal_handoff_unsupported` is unreachable from every valid body. secretarIA's
  post-booking hook stopped writing "abra a Pre-consulta" on the Portal conversation, because
  `Brain-Message-Frontend` now switches the patient to the pre-consult thread by itself the moment
  a hand-off fills it — no invitation, no tap, and no focus mode required. None of the three is
  deployed. Same-day, still NOT executed: `z_prompts/PROMPT_PORTAL_CALENDARIO_COMPONENTE.md`
  (`tasks/TASK-005`, a new availability endpoint this document does not describe yet — Portal
  only) and `z_prompts/PROMPT_WHATSAPP_FLOW_POC_CALENDARIO.md` (research/POC, no contract here
  unless it graduates).
