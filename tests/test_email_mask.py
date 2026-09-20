"""`core/email_mask.py` — the only shape of a patient's address allowed to leave (TASK-003 §3).

Two things are under test and they pull in opposite directions, which is why the edge cases
matter more than the happy path:

* the mask has to be RECOGNISABLE — a patient must be able to tell "yes, that's my gmail" —
  so the domain and the two outer characters are deliberately real;
* the mask must never be REVERSIBLE into the address, and must never accidentally echo more
  than the rule allows, which is what the short-local-part, `+tag` and malformed cases check.

Every address here is synthetic. No real patient address appears in this repo's tests.
"""

import pytest

from brain_api.core.email_mask import MASKED_PLACEHOLDER, mask_email


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        # The shape the contract spells out.
        ("ana@gmail.com", "a***a@gmail.com"),
        ("paciente@exemplo.com", "p***e@exemplo.com"),
        # A ONE-character local part keeps its single character and gains nothing: padding it
        # to look like two would be a lie about the address's length.
        ("a@gmail.com", "a***@gmail.com"),
        # Two characters: first and last are simply both of them. The mask is honest that
        # there is nothing in between rather than pretending there is.
        ("ab@gmail.com", "a***b@gmail.com"),
        ("ana.souza@clinica.com.br", "a***a@clinica.com.br"),
        # The domain is never touched, however many labels it has.
        ("ana@mail.interno.clinica.com.br", "a***a@mail.interno.clinica.com.br"),
        # `+tag` is NOT stripped and NOT surfaced: it is part of the local part like any
        # other character, and cutting at `+` would announce that a tag exists.
        ("ana+clinica@gmail.com", "a***a@gmail.com"),
        ("ana+@gmail.com", "a***+@gmail.com"),
        # Case is preserved, not normalised: the mask describes what is stored.
        ("Ana@GMAIL.com", "A***a@GMAIL.com"),
        # Digits / hyphens / underscores are just characters.
        ("1@x.io", "1***@x.io"),
        ("a_b-c1@sub.dominio.org", "a***1@sub.dominio.org"),
    ],
)
def test_mask_shape(address: str, expected: str) -> None:
    assert mask_email(address) == expected


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        # Precomposed accents survive as themselves.
        ("ácida@gmail.com", "á***a@gmail.com"),
        ("anã@gmail.com", "a***ã@gmail.com"),
        # DECOMPOSED input (NFD: "a" + U+0301) is composed first. Without the normalise step
        # the first character would come out as a bare "a" — silently dropping the accent —
        # and a decomposed final letter would leave a lone combining mark rendering as a
        # dotted circle. Both are wrong on screen, which is the whole point of the field.
        ("ácida@gmail.com", "á***a@gmail.com"),
        ("añ@gmail.com", "a***ñ@gmail.com"),
        # A one-character decomposed local part must still be ONE grapheme, not "a" alone.
        ("á@gmail.com", "á***@gmail.com"),
        # NFC IS NOT ENOUGH, and this is the case that proves it: "a" + acute + circumflex has
        # no precomposed codepoint, so NFC composes "á" and leaves the circumflex dangling.
        # Taking the last CODE POINT would print a lone ◌̂; the whole cluster must travel.
        ("á̂@gmail.com", "á̂***@gmail.com"),
        ("ab́̂@gmail.com", "a***b́̂@gmail.com"),
        # Non-Latin scripts are characters like any other — no transliteration.
        ("日本@例え.jp", "日***本@例え.jp"),
    ],
)
def test_mask_normalises_unicode(address: str, expected: str) -> None:
    assert mask_email(address) == expected


@pytest.mark.parametrize(
    "address",
    [
        # A local part that OPENS with a combining mark has no base to compose onto, so the
        # first "cluster" is the mark alone. Printing it would render as a dotted circle.
        "́@gmail.com",
        "́̂@gmail.com",
    ],
)
def test_mask_refuses_a_combining_mark_with_no_base(address: str) -> None:
    """Not describable, so nothing is described — `***`, never a bare mark."""
    assert mask_email(address) == MASKED_PLACEHOLDER


def test_mask_never_emits_a_leading_combining_mark() -> None:
    """The property behind the two cases above, stated as a property.

    Anything this function returns must be renderable: no piece of the output may START with a
    combining mark, whatever was fed in.
    """
    import unicodedata

    hostile = [
        "́@g.com",
        "á̂@g.com",
        "ab́̂@g.com",
        "́a@g.com",
        "à́̂@g.com",
    ]
    for address in hostile:
        out = mask_email(address)
        assert not unicodedata.combining(out[0]), (address, out)
        # `***` is the separator; neither side of it may open with a mark either.
        for piece in out.split(MASKED_PLACEHOLDER):
            assert not piece or not unicodedata.combining(piece[0]), (address, out)


def test_mask_does_not_raise_on_a_lone_surrogate() -> None:
    """`unicodedata.normalize` tolerates it here; the point is that nothing explodes."""
    assert mask_email("a\ud800@gmail.com").endswith("@gmail.com")


@pytest.mark.parametrize(
    "address",
    [
        None,
        "",
        "   ",
        # No separator at all.
        "not-an-address",
        # An empty local part or an empty domain describes nothing worth showing.
        "@gmail.com",
        "ana@",
        "@",
    ],
)
def test_mask_refuses_to_echo_anything_unusable(address: str | None) -> None:
    """Total function: anything that is not a usable address collapses to `***`.

    NEVER a partial echo and never an exception — this runs inside a response builder, and a
    malformed value must not be able to either crash the route or leak half of itself.
    """
    assert mask_email(address) == MASKED_PLACEHOLDER


def test_mask_splits_on_the_last_at_sign() -> None:
    """A quoted local part may legally contain `@`; a domain never does.

    So the LAST separator is the real one. Splitting on the first would treat the rest of the
    local part as the domain and print it in full — the exact leak this module exists to
    prevent.
    """
    assert mask_email('"weird@local"@gmail.com') == '"***"@gmail.com'


def test_mask_ignores_surrounding_whitespace() -> None:
    """A stray trailing space must not become the "last character" of the local part."""
    assert mask_email("  ana@gmail.com  ") == "a***a@gmail.com"


def test_mask_never_contains_the_local_part_middle() -> None:
    """The property the field exists to guarantee, stated as a property rather than a shape."""
    masked = mask_email("mariaaparecida@provedor.com.br")
    assert "ariaaparecid" not in masked
    assert masked == "m***a@provedor.com.br"
