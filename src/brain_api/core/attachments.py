"""Attachments on the Brain-Message channel — the ONE place their limits and checks live.

brain-api never keeps a file. It validates what a patient's browser uploads, relays it to the
product that stores it (`services/message_switchboard.py::send_attachment`) and streams it back
on request (`::open_media`). Everything in this module is therefore a CONTRACT, not a local
preference: secretarIA re-validates against the same numbers on its side (defence in depth —
it must never assume "brain-api already checked"), and a future PreCheck adaptation plugs into
these same functions instead of inventing its own limits
(docs/CHECKPOINT_brain_message_anexos.md).

Nothing here knows which PRODUCT a file is going to. Which products take one is routing
(`message_switchboard.ATTACHMENT_PRODUCTS`), not a property of the file.

The limits are constants, not settings, on purpose: an environment variable could make this
edge accept a file that secretarIA then refuses, and that disagreement would surface as an
opaque upstream error on a file the patient was told was fine.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

#: 20 MiB — the ceiling the Portal's earlier (never executed) attachment design had already
#: fixed for "what the Portal accepts, like WhatsApp". Stated in BYTES so every other repo
#: copies an integer (20971520), not an interpretation of "MB".
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
#: Room for the text fields (text <= 4000 characters, name, tap id), the part headers and
#: the boundaries. A request body larger than MAX_ATTACHMENT_BYTES + this is refused before
#: (Content-Length) or while (chunked) it is read — never parsed, never spooled.
MULTIPART_OVERHEAD_BYTES = 64 * 1024
#: Bytes needed to tell every accepted kind apart (WEBP's marker ends at offset 12).
SNIFF_BYTES = 16
#: What a media id may look like on the wire: a UUID (secretarIA) or any short opaque token a
#: future product uses. No dot, slash or percent, so an id can never walk the upstream path.
MEDIA_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
#: Longest display name kept, extension included.
MAX_FILENAME_CHARS = 120


@dataclass(frozen=True)
class AttachmentKind:
    """One accepted kind of file: the type it travels as, and the names it may carry."""

    content_type: str
    #: Canonical extension — used when a name has none, or a wrong one of the same family.
    extension: str
    #: Every extension a name of this kind keeps as-is.
    extensions: frozenset[str]
    #: "image" | "document": a name may slip WITHIN a family, never across one.
    family: str


JPEG = AttachmentKind("image/jpeg", "jpg", frozenset({"jpg", "jpeg", "jpe", "jfif"}), "image")
PNG = AttachmentKind("image/png", "png", frozenset({"png"}), "image")
WEBP = AttachmentKind("image/webp", "webp", frozenset({"webp"}), "image")
GIF = AttachmentKind("image/gif", "gif", frozenset({"gif"}), "image")
PDF = AttachmentKind("application/pdf", "pdf", frozenset({"pdf"}), "document")

#: The ONLY content types an attachment can carry across the mesh, in either direction: an
#: upload sniffed as anything else is refused, and the media route refuses to stream anything
#: else back (SVG is absent on purpose — it is an image that can carry script).
ALLOWED_KINDS: dict[str, AttachmentKind] = {
    kind.content_type: kind for kind in (JPEG, PNG, WEBP, GIF, PDF)
}
ALLOWED_CONTENT_TYPES: tuple[str, ...] = tuple(ALLOWED_KINDS)
_KIND_BY_EXTENSION = {ext: kind for kind in ALLOWED_KINDS.values() for ext in kind.extensions}

# --- Refusals: one table, so the reference doc and both repos read the same taxonomy -----

ATTACHMENT_EMPTY = "attachment_empty"
ATTACHMENT_TOO_LARGE = "attachment_too_large"
ATTACHMENT_TYPE_UNSUPPORTED = "attachment_type_unsupported"
ATTACHMENT_TYPE_MISMATCH = "attachment_type_mismatch"
ATTACHMENT_MALFORMED = "attachment_malformed"
ATTACHMENT_UNSUPPORTED_FOR_PRODUCT = "attachment_unsupported_for_product"
ATTACHMENT_NOT_FOUND = "attachment_not_found"

_MAX_MB = MAX_ATTACHMENT_BYTES // (1024 * 1024)

#: code -> (HTTP status, the sentence the patient sees). Every one is PERMANENT — the same
#: request resent unchanged never passes — so none is a 5xx a client would offer to retry
#: (brain-mesh-permanent-vs-transient-refusal): a 4xx, because the request itself is what
#: cannot pass, and never a 200, which a client would draw as "sent".
REFUSALS: dict[str, tuple[int, str]] = {
    ATTACHMENT_EMPTY: (422, "O arquivo está vazio. Escolha outro arquivo."),
    ATTACHMENT_TOO_LARGE: (
        413,
        f"O arquivo passa do limite de {_MAX_MB} MB. Envie um arquivo menor.",
    ),
    ATTACHMENT_TYPE_UNSUPPORTED: (
        415,
        "Tipo de arquivo não aceito. Envie uma imagem (JPG, PNG, WEBP ou GIF) ou um PDF.",
    ),
    ATTACHMENT_TYPE_MISMATCH: (
        422,
        "O conteúdo do arquivo não corresponde à extensão do nome. "
        "Confira o arquivo e envie de novo.",
    ),
    ATTACHMENT_MALFORMED: (422, 'Envio inválido: mande um único arquivo, no campo "file".'),
    ATTACHMENT_UNSUPPORTED_FOR_PRODUCT: (422, "Esta conversa ainda não aceita arquivos."),
    ATTACHMENT_NOT_FOUND: (404, "Arquivo não encontrado."),
}


class AttachmentRefused(Exception):
    """A refusal the caller can act on. Carries its CODE only — never a name, never a byte."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.status_code, self.message = REFUSALS[code]

    @property
    def detail(self) -> dict[str, Any]:
        """The HTTP error body: a stable code, the sentence, and the ceiling when it applies."""
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.code == ATTACHMENT_TOO_LARGE:
            body["max_bytes"] = MAX_ATTACHMENT_BYTES
        return body


@dataclass(frozen=True)
class CheckedAttachment:
    """An upload that passed `check_attachment`: its REAL kind, a safe name, its real size."""

    kind: AttachmentKind
    filename: str
    size_bytes: int


def sniff_kind(head: bytes) -> AttachmentKind | None:
    """The kind the CONTENT says it is, from its magic bytes — or None.

    Never the extension and never the browser's Content-Type: whoever sends the file picks
    both. Magic bytes are not a full decode (a JPEG header followed by garbage passes), which
    is enough for a transport: the file is only ever served back as this kind, with `nosniff`
    and a sandboxing CSP, and nothing on the way parses or executes it. PDF is matched at
    offset 0 only — stricter than readers, which scan the first KiB — so a file that merely
    CONTAINS a PDF header after other content (a polyglot) is refused.
    """
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return GIF
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return WEBP
    if head.startswith(b"%PDF-"):
        return PDF
    return None


# The characters reserved in file names on some OS (both separators are dealt with first).
_RESERVED = re.compile(r'[<>:"|?*]')
# Unicode categories with no business in a file name: controls (Cc, C0 and C1), format marks
# (Cf: the bidirectional overrides, zero-width and tag characters, the soft hyphen), private
# use (Co), lone surrogates (Cs) and unassigned code points (Cn) \u2014 everything that lets a name
# DISPLAY differently from what it is, or carry text a person cannot see. By CATEGORY, not by
# a list: a list missed seven such marks in review, and a list of invisible characters
# written into source is a hazard of its own.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})
_SPACES = re.compile(r"\s+")
# Only an alphabetic-led suffix counts as an extension, so "Exame 12.03.2026" is a name with
# dots rather than a file of type "2026".
_EXTENSION = re.compile(r"[a-z][a-z0-9]{0,9}")
_MEDIA_ID = re.compile(MEDIA_ID_PATTERN)


def _clean(raw: str | None) -> str:
    """The last path segment of `raw`, without what is unsafe in a header, a path or a UI.

    Both separators count, so a Windows path, `../../x.png` and `/etc/x.png` all reduce to
    `x.png`. Leading dots go too (no hidden, dot-file names).
    """
    name = unicodedata.normalize("NFC", raw or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES)
    name = _RESERVED.sub("", name)
    return _SPACES.sub(" ", name).strip().lstrip(".").strip()


def _split_name(name: str) -> tuple[str, str]:
    """`(stem, extension)`, the extension lower-cased, or `(name, "")` when it has none."""
    stem, dot, ext = name.rpartition(".")
    if dot and stem and _EXTENSION.fullmatch(ext.lower()):
        return stem, ext.lower()
    return name, ""


def extension_conflicts(raw_filename: str | None, kind: AttachmentKind) -> bool:
    """Whether the NAME promises a different kind of file than the CONTENT is.

    A conflict is an extension outside the accepted set, or one of another family (a `.jpg`
    that is really a PDF). A same-family slip — a PNG saved as `.jpg`, which phones and
    screenshot tools produce all the time — is not refused: `safe_filename` corrects it. A
    name without an extension never conflicts.
    """
    _, ext = _split_name(_clean(raw_filename))
    if not ext or ext in kind.extensions:
        return False
    claimed = _KIND_BY_EXTENSION.get(ext)
    return claimed is None or claimed.family != kind.family


def safe_filename(raw_filename: str | None, kind: AttachmentKind) -> str:
    """A display name safe to relay, store and render — and honest about the real kind.

    The extension is made to agree with `kind`, so the name shown in a chat can never promise
    a type the bytes are not. Empty after cleaning -> `anexo.<ext>`. This name may still
    carry PII (patients name files after themselves): it is relayed and displayed, and
    NEVER logged.
    """
    stem, ext = _split_name(_clean(raw_filename))
    if ext not in kind.extensions:
        ext = kind.extension
    stem = stem[: MAX_FILENAME_CHARS - len(ext) - 1].rstrip(" .") or "anexo"
    return f"{stem}.{ext}"


def is_media_id(value: str) -> bool:
    """Whether `value` is a well-formed media id (`MEDIA_ID_PATTERN`)."""
    return _MEDIA_ID.fullmatch(value) is not None


def check_attachment(head: bytes, size_bytes: int, raw_filename: str | None) -> CheckedAttachment:
    """The whole upload check, in the order a patient can act on. Raises `AttachmentRefused`.

    Size first (it needs no content), then the real kind from the first `SNIFF_BYTES` of the
    content, then whether the name agrees with it. What comes back — the SNIFFED kind and the
    cleaned name — is all that ever travels onward; the browser's own claims do not.
    """
    if size_bytes <= 0:
        raise AttachmentRefused(ATTACHMENT_EMPTY)
    if size_bytes > MAX_ATTACHMENT_BYTES:
        raise AttachmentRefused(ATTACHMENT_TOO_LARGE)
    kind = sniff_kind(head)
    if kind is None:
        raise AttachmentRefused(ATTACHMENT_TYPE_UNSUPPORTED)
    if extension_conflicts(raw_filename, kind):
        raise AttachmentRefused(ATTACHMENT_TYPE_MISMATCH)
    return CheckedAttachment(
        kind=kind, filename=safe_filename(raw_filename, kind), size_bytes=size_bytes
    )
