# CHECKPOINT — toque real no Portal: o campo atravessa o switchboard (2026-09-12)

Origem: `TECH/BRAIN/z_prompts/PROMPT_BRAIN_MESSAGE_PORTAL_INTERACTIVE_TAP.md`. Pares:
`secretarIA/docs/CHECKPOINT_portal_interactive_tap.md` (quem valida e roteia o id),
`Brain-Message-Frontend/docs/CHECKPOINT_portal_interactive_tap.md` (quem passa a mandá-lo).

**Estado: BUILT, `tests/test_patient_access.py` 31/31 verde (antes 26), UNCOMMITTED, não deployado.
Autorização: o prompt tem autorização explícita para tocar este repo; o DEPLOY exige confirmação do
usuário (nota §0 do prompt) e não foi feito.**

## §1 — O que mudou

| Onde | O quê |
|---|---|
| `schemas/patient_access.py` | `PatientMessageIn.interactive_reply_id: str | None` (1..256 chars, o mesmo limite da secretarIA). Continua `extra="forbid"`. |
| `services/message_switchboard.py` | `send_message(..., interactive_reply_id=None)`: entra no corpo **só no ramo `PRODUCT_SECRETARIA`**, e só quando presente. O ramo do PreCheck ficou intocado (é `extra="forbid"` sem o campo). |
| `api/patient_access.py` | `send_thread_message` repassa `payload.interactive_reply_id`. |

brain-api não interpreta o id: quem decide se ele é válido é a secretarIA, contra os cartões que ela
mesma ofereceu naquela conversa. Nada aqui permite à paciente apontar outra clínica ou outra
conversa: `tenant_id` e `patient_ref` seguem vindo da sessão (`get_current_patient`), nunca do corpo.

## §2 — Testes (em `tests/test_patient_access.py`)

- toque → o corpo enviado à secretarIA é exatamente `{tenant_id, external_id, text, interactive_reply_id}`;
  sem o campo, o corpo é byte a byte o de antes (não-regressão do cliente antigo);
- toque na aba PreCheck → o corpo enviado ao PreCheck é `{tenant_id, session_ref, text}` (o id é
  descartado nesse ramo; o rótulo ainda vai como `text`);
- `""`, 257 chars e chave extra (`tenant_id` no corpo) → 422 sem nenhuma chamada de rede.

`ruff check` limpo; `ruff format --check` acusa 3 arquivos que já estavam fora de formato no HEAD.

## §3 — Deploy (NÃO FEITO)

Ordem entre repos: **secretarIA → brain-api → frontend** (skill `frozen-contract-migration`). Este repo
é o meio da cadeia: com ele velho, um frontend novo toma 422 em todo toque; com ele novo e a
secretarIA velha, a secretarIA IGNORA o campo (`BrainMessageInbound` não é `extra="forbid"`) — seguro.

## §4 — Prova ao vivo LOCAL (2026-09-12)

brain-api real (uvicorn local, banco `brain-postgres` migrado) no meio da cadeia: o Portal em
Chrome → `POST /patient-access/threads/secretaria/messages` com `interactive_reply_id` → secretarIA
local honrou o id (toque na segunda de duas profissionais com título idêntico selecionou a segunda).
Detalhes em `secretarIA/docs/CHECKPOINT_portal_interactive_tap.md` §7.
