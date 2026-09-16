#!/usr/bin/env python
#
"""Test that minted internal_identifier values are stable and distinct."""

# 3rd party imports
import pytest
import pytest_check as check

# Project imports
from tripsy_exim.models import (
    FIRST_GENERATION,
    IDENTIFIER_PREFIX,
    generation_of,
    is_minted,
    mint,
    next_generation,
    scratch_namespace,
)


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


########################################################################
########################################################################
#
class TestGeneration:
    """
    Tests for the generation segment.

    Tripsy never releases an identifier, so a record whose identifier was
    spent by something since deleted can only be imported again under a
    different one.  The generation is how that is done without giving up
    deriving identity from the source.
    """

    ####################################################################
    #
    @pytest.mark.parametrize(
        "namespace,generation",
        [
            pytest.param("ics", FIRST_GENERATION, id="plain"),
            pytest.param("tripit-json", FIRST_GENERATION, id="hyphenated"),
            pytest.param("scratch-01", FIRST_GENERATION, id="digit-token"),
            pytest.param(
                "scratch-g09", FIRST_GENERATION, id="generation-like-token"
            ),
            pytest.param(scratch_namespace("01"), 2, id="a-scratch-run"),
            pytest.param("ics", 9, id="the-last-single-digit"),
            pytest.param("ics", 10, id="two-digits"),
            pytest.param("ics", 42, id="well-past-the-boundary"),
        ],
    )
    def test_the_generation_sits_between_namespace_and_digest(
        self, namespace: str, generation: int
    ) -> None:
        """
        GIVEN: a namespace, some of which could be read as a generation
        WHEN:  an identifier is minted and its generation read back
        THEN:  the segment is where it belongs and says what it should

        The shape is asserted literally because the segment cannot be
        added later: doing so would re-key everything already imported.
        A bare number would be ambiguous -- it is valid hex and a legal
        scratch run token alike -- which is why it is letter-tagged.
        """
        identifier = mint(namespace, "uid-1", generation=generation)

        check.is_true(
            identifier.startswith(
                f"{IDENTIFIER_PREFIX}-{namespace}-g{generation:02d}-"
            ),
            identifier,
        )
        check.equal(generation_of(identifier), generation)

    ####################################################################
    #
    def test_generations_of_one_record_share_a_digest(self) -> None:
        """
        GIVEN: two generations of the same source record
        WHEN:  their digests are compared
        THEN:  they match, so the two are recognisably one record

        The counter sits outside the hash for exactly this reason.
        """
        first = mint("ics", "uid-1")
        later = mint("ics", "uid-1", generation=3)

        assert first.rsplit("-", 1)[1] == later.rsplit("-", 1)[1]
        assert first != later

    ####################################################################
    #
    def test_the_next_generation_is_the_same_record(self) -> None:
        """
        GIVEN: an identifier
        WHEN:  the next generation is taken
        THEN:  only the counter moves
        """
        first = mint("tripit-json", "uid-1")

        assert next_generation(first) == mint(
            "tripit-json", "uid-1", generation=2
        )
        assert next_generation(next_generation(first)) == mint(
            "tripit-json", "uid-1", generation=3
        )

    ####################################################################
    #
    def test_scratch_namespaces_keep_their_generation(self) -> None:
        """
        GIVEN: a throwaway run whose token is itself all digits
        WHEN:  its identifier is read
        THEN:  the namespace and the generation stay separable
        """
        identifier = mint(scratch_namespace("01"), "uid-1")

        check.is_true(identifier.startswith(f"{IDENTIFIER_PREFIX}-scratch-01-"))
        check.equal(generation_of(identifier), FIRST_GENERATION)

    ####################################################################
    #
    @pytest.mark.parametrize(
        "value",
        [None, "", "not-an-identifier", "txim-ics-0123456789abcdef"],
        ids=["none", "empty", "arbitrary", "ungenerationed"],
    )
    def test_anything_without_a_generation_reads_as_none(
        self, value: str | None
    ) -> None:
        """
        GIVEN: something that is not an identifier this mints
        WHEN:  its generation is read
        THEN:  None comes back rather than a wrong number
        """
        assert generation_of(value) is None

    ####################################################################
    #
    def test_bumping_a_foreign_identifier_is_refused(self) -> None:
        """
        GIVEN: an identifier this did not mint
        WHEN:  the next generation is asked for
        THEN:  it raises rather than inventing one
        """
        with pytest.raises(ValueError):
            next_generation("some-other-service-identifier")

    ####################################################################
    #
    @pytest.mark.parametrize("generation", [0, -1])
    def test_a_generation_below_one_is_refused(self, generation: int) -> None:
        """
        GIVEN: a generation below the first
        WHEN:  an identifier is minted
        THEN:  it raises, since there is no generation zero
        """
        with pytest.raises(ValueError):
            mint("ics", "uid-1", generation=generation)
