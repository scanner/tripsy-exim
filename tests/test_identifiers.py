#!/usr/bin/env python
#
"""Test that minted internal_identifier values are stable and distinct."""

# 3rd party imports
import pytest
import pytest_check as check

# Project imports
from tripsy_exim.models import IDENTIFIER_PREFIX, is_minted, mint


########################################################################
########################################################################
#
class TestMint:
    """Tests for deterministic identifier minting."""

    ####################################################################
    #
    def test_minting_is_deterministic_and_well_formed(self) -> None:
        """
        GIVEN: one source record
        WHEN:  an identifier is minted from it
        THEN:  it is reproducible, tagged as ours, and long enough for
               trip-level duplicate suppression to engage
        """
        identifier = mint("ics", "uid-1")

        check.equal(identifier, mint("ics", "uid-1"), "reproducible")
        check.greater(len(identifier), 5, "over the suppression threshold")
        check.is_true(
            identifier.startswith(f"{IDENTIFIER_PREFIX}-ics-"),
            "prefixed and namespaced",
        )

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
        THEN:  only ours is recognised, since the archive keys them apart
        """
        assert is_minted(identifier) is expected
