#!/usr/bin/env python
#
"""Test that minted internal_identifier values are stable and distinct."""

# 3rd party imports
import pytest

# Project imports
from tripsy_exim.models import IDENTIFIER_PREFIX, is_minted, mint


########################################################################
########################################################################
#
class TestMint:
    """Tests for deterministic identifier minting."""

    ####################################################################
    #
    def test_same_source_gives_the_same_identifier(self) -> None:
        """
        GIVEN: one source record
        WHEN:  an identifier is minted from it twice
        THEN:  both are identical, so a re-import is a no-op
        """
        assert mint("ics", "uid-1") == mint("ics", "uid-1")

    ####################################################################
    #
    @pytest.mark.parametrize(
        "left,right",
        [
            pytest.param(
                ("ics", "uid-1"), ("ics", "uid-2"), id="different-uid"
            ),
            pytest.param(
                ("ics", "uid-1"), ("tripit", "uid-1"), id="different-source"
            ),
            pytest.param(
                ("ics", "a", "bc"), ("ics", "ab", "c"), id="part-boundary"
            ),
        ],
    )
    def test_different_sources_give_different_identifiers(
        self, left: tuple[str, ...], right: tuple[str, ...]
    ) -> None:
        """
        GIVEN: two distinct source records
        WHEN:  identifiers are minted from each
        THEN:  they differ, so neither suppresses the other's create
        """
        assert mint(*left) != mint(*right)

    ####################################################################
    #
    def test_identifier_clears_the_five_character_threshold(self) -> None:
        """
        GIVEN: a minted identifier
        WHEN:  its length is measured
        THEN:  it is well over 5 characters, which trip-level duplicate
               suppression requires
        """
        identifier = mint("ics", "uid-1")

        assert len(identifier) > 5
        assert identifier.startswith(f"{IDENTIFIER_PREFIX}-ics-")

    ####################################################################
    #
    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(("", "uid-1"), id="empty-namespace"),
            pytest.param(("ics",), id="no-parts"),
            pytest.param(("ics", ""), id="empty-part"),
        ],
    )
    def test_useless_input_is_rejected(self, args: tuple[str, ...]) -> None:
        """
        GIVEN: input that cannot identify a record
        WHEN:  an identifier is minted
        THEN:  a ValueError is raised rather than a collision-prone value
        """
        with pytest.raises(ValueError):
            mint(*args)


########################################################################
########################################################################
#
class TestIsMinted:
    """Tests for recognising our own identifiers."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "identifier,expected",
        [
            pytest.param(mint("ics", "uid-1"), True, id="minted"),
            pytest.param("some-app-identifier", False, id="foreign"),
            pytest.param("", False, id="empty"),
            pytest.param(None, False, id="absent"),
        ],
    )
    def test_recognises_only_our_own(
        self, identifier: str | None, expected: bool
    ) -> None:
        """
        GIVEN: an identifier from us, from elsewhere, or missing
        WHEN:  it is tested
        THEN:  only ours is recognised
        """
        assert is_minted(identifier) is expected
