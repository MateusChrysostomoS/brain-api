"""The clinic's patient invite: a short code a person can type, and the parser that turns
whatever a patient pastes into ONE lookup key.

Neither the code nor the clinic's UUID is a credential. Both only NAME a clinic: adding it
to an account needs an inbox already proven by a code, and a clinic with the Brain-Message
channel off answers exactly like one that does not exist
(`services/patient_access.py::resolve_invite`). So the format only has to be typeable and
hard to confuse: 8 symbols from an alphabet without the look-alikes 0/O, 1/I/L and U
(next to V) — 30^8 ≈ 6.6 × 10^11 codes, which is also what keeps a guessed code from
landing on a real clinic in practice.

What a patient may paste, all resolving to the same clinic:
  - the code itself, any case, with spaces or hyphens (`abcd-2345`);
  - the clinic's link, `<portal>/clinicas/?convite=<code>`;
  - an old portal link, `<portal>/conversa/?clinica=<uuid>` (still an invite, owner's call);
  - the bare UUID.
"""

import re
import secrets
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from brain_api.config import get_settings

ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
# Longer than any link the portal builds, short enough that parsing is never the costly part.
MAX_INVITE_LENGTH = 512
# Query keys that may carry the invite, in the order they are tried.
INVITE_QUERY_KEYS = ("convite", "clinica", "codigo", "code", "invite")

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_SEPARATORS_RE = re.compile(r"[\s\-]+")


def generate_invite_code() -> str:
    """A fresh code from `secrets` (uniformly over the alphabet)."""
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def normalize_invite_code(raw: str) -> str | None:
    """The stored spelling of a typed code, or `None` when it cannot be one."""
    code = _SEPARATORS_RE.sub("", raw).upper()
    if len(code) != CODE_LENGTH or any(ch not in ALPHABET for ch in code):
        return None
    return code


def _single(value: str) -> UUID | str | None:
    value = value.strip()
    if _UUID_RE.fullmatch(value):
        return UUID(value)
    return normalize_invite_code(value)


def parse_invite(raw: str) -> UUID | str | None:
    """A clinic UUID, a normalized code, or `None` — never an error.

    Anything shaped like a link is read by its query (the keys above) and, failing that, by
    its last path segment; anything else is read as a code or a UUID on its own.
    """
    text = raw.strip()
    if not text or len(text) > MAX_INVITE_LENGTH:
        return None
    if not any(mark in text for mark in ("/", "?", "=")):
        return _single(text)
    parts = urlsplit(text if "://" in text else f"https://portal/{text.lstrip('/')}")
    query = parse_qs(parts.query)
    for key in INVITE_QUERY_KEYS:
        for value in query.get(key, []):
            found = _single(value)
            if found is not None:
                return found
    segments = [segment for segment in parts.path.split("/") if segment]
    return _single(segments[-1]) if segments else None


def invite_link(code: str | None) -> str | None:
    """The link a clinic hands out, or `None` while `BRAIN_MESSAGE_PORTAL_URL` is unset."""
    base = get_settings().BRAIN_MESSAGE_PORTAL_URL.strip().rstrip("/")
    if not base or not code:
        return None
    return f"{base}/clinicas/?convite={code}"
