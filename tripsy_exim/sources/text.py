#!/usr/bin/env python
#
"""
Recovering text TripIt wrote through the wrong codec.

TripIt has no consistent encoding for non-English text.  What it
exports is UTF-8 that was written out through cp1252, so
`日本の旅行` arrives as `æ—¥æœ¬ã®æ—…è¡Œ`.
These functions read back the characters that were meant and hand
over an ordinary Python `str`, which is the only representation the
rest of the code should ever see.

The encoding is mixed, which is what makes it awkward.  cp1252 leaves
five bytes undefined -- 0x81, 0x8d, 0x8f, 0x90, 0x9d -- and those came
through as raw latin-1, so neither codec round-trips a whole string.
Recovery works a character at a time.

This is not cosmetic.  A trip name is a join key, so an unrecovered
copy matches nothing and the problem shows up as missing records
rather than as odd-looking text.  Recovery belongs at parse time,
before anything is keyed on a string.

One pass is all this does.  A string that went through the encoding
twice comes back half-read -- characters that were recovered beside
ones that were not -- and is then left alone, because telling that
apart from text legitimately containing those characters is guesswork.
The real export holds a few dozen of them, none a trip name.

Only the JSON export needs this.  The `.ics` exports are clean UTF-8.
"""

# system imports
import re
from typing import Any

# The bytes cp1252 does not define.  A mojibake string carries these as
# raw latin-1, so encoding it back has to fall through to the byte.
#
_UNDEFINED_IN_CP1252 = frozenset({0x81, 0x8D, 0x8F, 0x90, 0x9D})

# A non-breaking space is C2 A0.  Read through cp1252 that becomes
# U+00C2 followed by U+00A0, and something downstream then flattened
# the U+00A0 to an ordinary space -- leaving the U+00C2 with nothing to
# pair against.  An optional space ahead of it is taken too, so the
# result is the single space the character stood for rather than two.
#
_ORPHAN_LEAD = re.compile(" ?\u00c2 ")


####################################################################
#
def repair_mojibake(text: str) -> str:
    """
    Read back one round of UTF-8 bytes that were decoded as cp1252.

    Recovery is attempted and then checked rather than detected in
    advance: the string is encoded back to the bytes it came from and
    only kept if those bytes are valid UTF-8.  Anything that fails at
    either step is returned untouched, so this is safe to run over text
    that was already correct and safe to run twice.

    Text in a script cp1252 cannot represent is left alone by the first
    step, which is what keeps already-correct Japanese from being
    mangled: `日` has no cp1252 encoding, so the string short-circuits.

    Args:
        text: A string, however TripIt happened to encode it.

    Returns:
        The text as it was meant to read, or the original when it
        was already correct or cannot be read back.
    """
    if text.isascii():
        return text

    original = bytearray()
    for char in text:
        try:
            original += char.encode("cp1252")
        except UnicodeEncodeError:
            point = ord(char)
            if point not in _UNDEFINED_IN_CP1252:
                # A character cp1252 cannot hold at all.  The string is
                # real text in another script, not mojibake.
                #
                return text
            original.append(point)

    try:
        return original.decode("utf-8")
    except UnicodeDecodeError:
        return text


####################################################################
#
def drop_orphan_leads(text: str) -> str:
    """
    Remove a lead byte left stranded where a space used to be.

    This runs after `repair_mojibake` and cleans up what that cannot:
    the character it would need to pair with is gone, so no amount of
    decoding recovers it and the only honest options are to keep the
    stray mark or drop it.  Dropping is safe here precisely because what
    it stood for was a space, so the text reads as it was meant to.

    Unlike the decoding, this is a judgement rather than a reversal.  It
    is kept separate for that reason.

    Args:
        text: Text that has already been through `repair_mojibake`.

    Returns:
        The text with stranded lead bytes replaced by the space they
        stood for.
    """
    if text.isascii():
        return text
    return _ORPHAN_LEAD.sub(" ", text)


####################################################################
#
def recover(text: str) -> str:
    """
    Read a string back the way it was meant, as far as that is possible.

    Args:
        text: A string, however TripIt happened to encode it.

    Returns:
        The text decoded, then cleaned of anything the decoding had to
        leave behind.
    """
    return drop_orphan_leads(repair_mojibake(text))


####################################################################
#
def repair_strings(value: Any) -> Any:
    """
    Recover every string in a parsed JSON structure.

    Dicts and lists are rebuilt rather than mutated so a caller can keep
    the raw document beside the recovered one, which the coverage report
    wants.  Keys are repaired too: TripIt's are ASCII today, but a key
    is a string and nothing guarantees that stays true.

    Args:
        value: Any value decoded from JSON.

    Returns:
        The same structure with its strings recovered.
    """
    if isinstance(value, str):
        return recover(value)
    if isinstance(value, dict):
        return {
            repair_strings(key): repair_strings(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [repair_strings(item) for item in value]
    return value
