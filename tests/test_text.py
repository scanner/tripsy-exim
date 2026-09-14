#!/usr/bin/env python
#
"""
Test recovering text TripIt encoded through cp1252.

The inputs here are produced by re-running TripIt's own encoding
rather than by pasting anything out of a real export, so the
fixtures stay synthetic and document exactly what TripIt does.
"""

# system imports
from typing import Any

# 3rd party imports
import pytest

# Project imports
from tripsy_exim.sources.text import (
    drop_orphan_leads,
    recover,
    repair_mojibake,
    repair_strings,
)

# The bytes cp1252 leaves undefined, which a lenient decoder passes
# through as latin-1.  Python's own cp1252 decoder raises on them, so
# the encoding has to be reproduced a byte at a time.
#
UNDEFINED = {0x81, 0x8D, 0x8F, 0x90, 0x9D}


####################################################################
#
def mojibake(text: str) -> str:
    """Encode text the way TripIt does: UTF-8 written through cp1252."""
    out = []
    for byte in text.encode("utf-8"):
        if byte in UNDEFINED:
            out.append(chr(byte))
        else:
            out.append(bytes([byte]).decode("cp1252"))
    return "".join(out)


########################################################################
########################################################################
#
class TestRepairMojibake:
    """Tests for repairing a single string."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "text",
        [
            "2012年4月 日本の旅行",
            "新幹線",
            "5-chōme-16-1 Nishinakajima",
            "〒710-1101 Okayama",
            "Café Zoë",
            "Kan-onji Station",
        ],
        ids=[
            "trip-name-with-undefined-byte",
            "shinkansen",
            "macron",
            "postal-mark",
            "latin-accents",
            "ascii-with-hyphen",
        ],
    )
    def test_exported_text_reads_back(self, text: str) -> None:
        """
        GIVEN: text encoded the way TripIt encodes it
        WHEN:  it is repaired
        THEN:  the original text comes back
        """
        assert repair_mojibake(mojibake(text)) == text

    ####################################################################
    #
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Honolulu, HI, November 2011",
            "2012年4月 日本の旅行",
            "新幹線",
            "〒710-1101 Okayama",
        ],
        ids=["empty", "ascii", "japanese", "kanji", "postal-mark"],
    )
    def test_correct_text_is_untouched(self, text: str) -> None:
        """
        GIVEN: text that came through correctly
        WHEN:  it is repaired
        THEN:  it is returned unchanged

        This is the guard that matters most.  Plenty of the export is
        already right, and a recovery that mangles correct Japanese
        would cost more than it gains.
        """
        assert repair_mojibake(text) == text

    ####################################################################
    #
    def test_repair_is_idempotent(self) -> None:
        """
        GIVEN: a string TripIt encoded
        WHEN:  it is repaired twice
        THEN:  the second pass changes nothing
        """
        once = repair_mojibake(mojibake("日本の旅行"))
        assert repair_mojibake(once) == once

    ####################################################################
    #
    def test_undecodable_bytes_are_left_alone(self) -> None:
        """
        GIVEN: a latin-1 string whose bytes are not valid UTF-8
        WHEN:  it is repaired
        THEN:  the original is returned rather than an exception raised
        """
        assert repair_mojibake("Ã") == "Ã"


########################################################################
########################################################################
#
class TestRepairStrings:
    """Tests for walking a parsed JSON document."""

    ####################################################################
    #
    def test_nested_structure_is_repaired(self) -> None:
        """
        GIVEN: a document shaped like the export, encoded as TripIt does
        WHEN:  the document is repaired
        THEN:  every string is repaired and non-strings are preserved
        """
        document: dict[str, Any] = {
            "TripData": {"display_name": mojibake("2012年4月 日本の旅行")},
            "Objects": [
                {"display_name": mojibake("新幹線"), "is_purchased": "true"},
                {"display_name": "Flight", "count": 3, "ok": True},
            ],
            "missing": None,
        }

        assert repair_strings(document) == {
            "TripData": {"display_name": "2012年4月 日本の旅行"},
            "Objects": [
                {"display_name": "新幹線", "is_purchased": "true"},
                {"display_name": "Flight", "count": 3, "ok": True},
            ],
            "missing": None,
        }

    ####################################################################
    #
    def test_the_original_document_is_not_mutated(self) -> None:
        """
        GIVEN: a document encoded the way TripIt encodes
        WHEN:  it is repaired
        THEN:  the input is unchanged, so raw and recovered compare
        """
        exported = mojibake("新幹線")
        document = {"display_name": exported}

        repair_strings(document)

        assert document == {"display_name": exported}


########################################################################
########################################################################
#
class TestDropOrphanLeads:
    """Tests for clearing what the decoding has to leave behind."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "text,expected",
        [
            (
                "Tokyo Metro Marunouchi Line \u00c2 for\u00c2 OGIKUBO",
                "Tokyo Metro Marunouchi Line for OGIKUBO",
            ),
            ("Departure\u00c2 track\u00c2 No.", "Departure track No."),
            (
                "JR Sagano Line \u00c2 for\u00c2 KYOTO",
                "JR Sagano Line for KYOTO",
            ),
        ],
        ids=["with-leading-space", "without", "another-line"],
    )
    def test_a_stranded_lead_becomes_the_space_it_stood_for(
        self, text: str, expected: str
    ) -> None:
        """
        GIVEN: text where a non-breaking space lost its trailing byte
        WHEN:  the stranded lead is dropped
        THEN:  a single space is left, not two

        These are rail and bus operator names, which is the field the
        retype review reads most closely.
        """
        assert drop_orphan_leads(text) == expected

    ####################################################################
    #
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "plain ascii text",
            "\u65e5\u672c\u306e\u65c5\u884c",
            "Caf\u00e9 Zo\u00eb",
            "\u00c2ngstr\u00f6m",
        ],
        ids=["empty", "ascii", "japanese", "accents", "leading-A-circumflex"],
    )
    def test_text_without_the_pattern_is_untouched(self, text: str) -> None:
        """
        GIVEN: text carrying no stranded lead
        WHEN:  it is cleaned
        THEN:  it is returned unchanged

        The last case matters: a word may legitimately begin with the
        same character, and only the trailing space makes it a stranded
        lead rather than a letter.
        """
        assert drop_orphan_leads(text) == text


########################################################################
########################################################################
#
class TestRecover:
    """Tests for the two steps run together."""

    ####################################################################
    #
    def test_a_non_breaking_space_survives_the_whole_journey(self) -> None:
        """
        GIVEN: text whose non-breaking space went through TripIt's
               encoding and then had its trailing byte flattened
        WHEN:  it is recovered
        THEN:  it reads as it was meant to

        This reproduces the whole path end to end rather than asserting
        against a pasted constant.
        """
        original = "JR Sagano Line\u00a0for\u00a0KYOTO"

        encoded = "".join(
            chr(b) if b in UNDEFINED else bytes([b]).decode("cp1252")
            for b in original.encode("utf-8")
        )
        flattened = encoded.replace("\u00a0", " ")

        assert recover(flattened) == "JR Sagano Line for KYOTO"

    ####################################################################
    #
    def test_recovery_still_decodes_ordinary_text(self) -> None:
        """
        GIVEN: text that only needs decoding, with nothing stranded
        WHEN:  it is recovered
        THEN:  decoding still happens
        """
        assert recover(mojibake("\u65b0\u5e79\u7dda")) == "\u65b0\u5e79\u7dda"

    ####################################################################
    #
    def test_repair_alone_leaves_the_stranded_lead(self) -> None:
        """
        GIVEN: text with a stranded lead
        WHEN:  only the decoding step runs
        THEN:  the lead is still there, which is why the second step exists
        """
        text = "JR Sagano Line \u00c2 for\u00c2 KYOTO"

        assert "\u00c2" in repair_mojibake(text)
        assert "\u00c2" not in recover(text)
