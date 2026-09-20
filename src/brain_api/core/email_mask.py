"""The ONE shape a patient's e-mail address is allowed to leave this service in.

The full address never leaves brain-api — not in a response, not in a log line, not in an
error detail (`api/patient_access.py`'s PII note, `docs/CHECKPOINT_portal_sessao_pendente.md`).
But the notice that asks for the six-digit code has to say WHICH inbox it went to, or the
patient cannot tell a typo from a slow mail server. That is what this module is for, and it
is the only exception.

THE RULE (TASK-003 §3): first character of the local part, `***`, last character of the
local part, `@`, and the domain **whole**. A one-character local part becomes `a***@dominio`
— it has nothing left to hide, and inventing a second character to hide would be a lie about
the address's length.

WHY THE DOMAIN IS NOT MASKED. The mask exists so a person RECOGNISES their own inbox, and the
domain is what does that ("ah, the gmail one"). Masking it would cost the notice its whole
purpose while protecting nothing: an attacker who already holds the mask can guess three
providers and be right most of the time. What identifies a person is the local part, and that
is what is hidden.

WHAT IS DELIBERATELY *NOT* DONE — both would leak rather than protect:

* **No `+tag` stripping.** Cutting at `+` would announce that a tag exists and how long the
  base address is. A tag is just characters in the local part and is masked like any other.
* **No dot-folding / canonicalisation.** Gmail treats `a.na@` and `ana@` as one inbox;
  "correcting" the address here would reveal the canonical form of an address the patient
  never typed that way.

**The outer characters are grapheme clusters, not code points**, and NFC alone is not enough to
make that true. A decomposed `á` is `a` + U+0301, so slicing code point 0 would silently drop
the accent — NFC fixes that one. But NFC composes only what Unicode has a precomposed character
for: `a` + acute + circumflex composes to `á` and leaves the circumflex **dangling**, and a
local part that begins with a combining mark has no base to compose onto at all. Either way a
bare combining mark ends up alone in the output and renders as a dotted circle (◌̂). So after
normalising, the first and last "characters" are taken as base-plus-its-combining-marks, and a
mark with no base of its own means the address is not describable — `***`.

Pure and total: no I/O, no settings, no exceptions. Anything that is not a usable address
collapses to `***` — never a partial echo of whatever was passed in.
"""

import unicodedata

#: What leaves when there is no address to describe, or the value is not one. Deliberately
#: NOT an empty string: a consumer rendering "enviado para " with nothing after it looks like
#: a bug, while `***` reads as "we are not telling you here".
MASKED_PLACEHOLDER = "***"


def _clusters(text: str) -> list[str]:
    """`text` split into base-plus-combining-marks groups — a poor man's grapheme clusters.

    Deliberately NOT the `regex` package's `\\X`: pulling a third-party dependency into a
    59-line helper is a worse trade than handling the one class of cluster that actually
    occurs in an e-mail local part. A leading combining mark has no base, so it becomes a
    group of its own, and `mask_email` treats that as "not describable" rather than printing
    it bare.
    """
    groups: list[str] = []
    for char in text:
        if groups and unicodedata.combining(char):
            groups[-1] += char
        else:
            groups.append(char)
    return groups


def mask_email(address: str | None) -> str:
    """`ana@gmail.com` -> `a***a@gmail.com`; `a@gmail.com` -> `a***@gmail.com`.

    Splits on the LAST `@` (`rpartition`), because a quoted local part may legally contain
    one and the domain never does — so the last separator is always the real one.
    """
    if not address:
        return MASKED_PLACEHOLDER
    local, separator, domain = address.strip().rpartition("@")
    if not separator or not local or not domain:
        return MASKED_PLACEHOLDER
    groups = _clusters(unicodedata.normalize("NFC", local))
    first, last = groups[0], groups[-1]
    # A "cluster" that is only combining marks means the local part opened with one: there is
    # no character to show, and showing the mark alone would render as a dotted circle.
    if unicodedata.combining(first[0]) or unicodedata.combining(last[0]):
        return MASKED_PLACEHOLDER
    if len(groups) == 1:
        return f"{first}{MASKED_PLACEHOLDER}@{domain}"
    return f"{first}{MASKED_PLACEHOLDER}{last}@{domain}"
