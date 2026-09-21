#!/usr/bin/env python
#
"""
Test placing an endpoint from what the archive already knows.

The case this is for needs two trips: one that placed an airport and one
that named the same airport and did not.  `two_trips` builds exactly
that, with real coordinates for each airport so a test can assert that
the *right* position moved rather than that some position did.
"""

# system imports
from collections.abc import Callable

# 3rd party imports
import pytest
import pytest_check as check

# Project imports
from tests import tripit_builder as b
from tests.places import (
    HND,
    KIX,
    NRT,
    ONE_BAY,
    SFO,
    SFO_CENTROID,
    SFO_RUNWAY,
    YVR,
)
from tripsy_exim.store import Archive
from tripsy_exim.sync import stage_export
from tripsy_exim.sync.backfill import gaps
from tripsy_exim.sync.infer import (
    DISAGREE_KM,
    NOTHING_PLACES_IT,
    SEGMENTS_DISAGREE,
    Position,
    exact_key,
    filled_rows,
    inferences,
    positions_by_key,
    settle,
)
from tripsy_exim.sync.worklist import apply_rows


####################################################################
#
@pytest.fixture
def two_trips(archive: Archive) -> Callable[..., Archive]:
    """
    One trip that placed an airport, and one that did not.

    The unplaced trip names the same code, which is the whole of the
    case: the answer is already on disk, on another trip.
    """

    def build(placed_at: tuple[float, float] = NRT) -> Archive:
        stage_export(
            archive,
            b.export(
                b.trip(
                    name="Placed trip",
                    objects=[
                        b.flight(
                            frm="Narita",
                            to="Osaka",
                            frm_at=placed_at,
                            to_at=KIX,
                        )
                    ],
                ),
                b.trip(
                    name="Unplaced trip",
                    start="2024-08-01",
                    end="2024-08-04",
                    objects=[
                        b.flight(frm="Narita", to="Vancouver", placed=False)
                    ],
                ),
            ),
        )
        return archive

    return build


########################################################################
########################################################################
#
class TestExactKey:
    """Tests for what counts as a key two endpoints can share."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "label,expected",
        [
            ("NAR", "NAR"),
            ("  NAR  ", "NAR"),
            ("Shin-Osaka Station", None),
            ("Example Hotel", None),
            ("nar", None),
            ("NARI", None),
            ("N1R", None),
            ("", None),
            (None, None),
        ],
    )
    def test_only_a_bare_three_letter_code_is_a_key(
        self, label: str | None, expected: str | None
    ) -> None:
        """
        GIVEN: what an export called a place
        WHEN:  it is asked whether that is a key
        THEN:  only an airport code is one

        Free text is not a key: two spellings of one station are two
        keys, and one spelling can be two stations.  Matching on those
        would place things confidently and wrongly.
        """
        check.equal(exact_key(label), expected)


########################################################################
########################################################################
#
class TestSettle:
    """Tests for what one key's observations amount to."""

    ####################################################################
    #
    def test_the_commonest_position_wins(self) -> None:
        """
        GIVEN: one position recorded three times and an odd one once
        WHEN:  the observations are settled
        THEN:  the common one is the answer

        The mode rather than the first, so a single odd record cannot
        move an airport.
        """
        position, agreed, refusal = settle([SFO, SFO_CENTROID, SFO, SFO])

        check.equal(position, SFO)
        check.equal(agreed, 3)
        check.equal(refusal, "")

    ####################################################################
    #
    def test_positions_far_apart_are_refused(self) -> None:
        """
        GIVEN: one key observed in two places far apart
        WHEN:  the observations are settled
        THEN:  it is refused rather than answered

        A code used for two places cannot be settled by taking the
        commonest: that answers confidently and wrongly.  The mode
        protects against one odd record, not against a key that means
        two things.
        """
        position, _, refusal = settle([NRT, NRT, YVR])

        check.is_none(position)
        check.is_in(SEGMENTS_DISAGREE, refusal)
        check.is_in("km apart", refusal, "and says how far")

    ####################################################################
    #
    def test_two_airports_serving_one_city_are_refused(self) -> None:
        """
        GIVEN: one code observed at Narita and at Haneda
        WHEN:  the observations are settled
        THEN:  it is refused

        The realistic shape of the hazard: not a code reused across the
        world, but one reused across a metro area.  Sixty kilometres is
        far enough to be a different airport and close enough that a
        careless threshold would wave it through.
        """
        position, _, refusal = settle([NRT, HND])

        check.is_none(position)
        check.is_in(SEGMENTS_DISAGREE, refusal)

    ####################################################################
    #
    @pytest.mark.parametrize(
        "pair,apart_km",
        [
            ((ONE_BAY[0], ONE_BAY[1]), 17),
            ((ONE_BAY[1], ONE_BAY[2]), 47),
            ((ONE_BAY[0], ONE_BAY[2]), 49),
        ],
        ids=["SFO-OAK", "OAK-SJC", "SFO-SJC"],
    )
    def test_two_airports_serving_one_bay_are_refused(
        self, pair: tuple[Position, Position], apart_km: int
    ) -> None:
        """
        GIVEN: one code observed at two airports serving one region
        WHEN:  the observations are settled
        THEN:  it is refused

        The tightest real case there is, and the one that set the
        threshold: all three Bay Area airports sit within fifty
        kilometres of each other, so a threshold picked by reasoning
        rather than measured against them waves every pair through.
        """
        position, _, refusal = settle([pair[0], pair[1]])

        check.is_none(position, f"{apart_km}km apart is two airports")
        check.is_in(SEGMENTS_DISAGREE, refusal)

    ####################################################################
    #
    def test_one_airport_is_not_refused_for_disagreeing_slightly(
        self,
    ) -> None:
        """
        GIVEN: published coordinate variants for a single airport
        WHEN:  the observations are settled
        THEN:  they are treated as one place

        The other side of the same threshold.  Terminal, centroid and
        runway differ by around a kilometre, and a guard that fired on
        that would refuse every airport in a real archive.
        """
        position, _, refusal = settle([SFO, SFO_CENTROID, SFO_RUNWAY])

        check.is_not_none(position)
        check.equal(refusal, "")

    ####################################################################
    #
    def test_nothing_observed_is_said_so(self) -> None:
        """
        GIVEN: a key nothing in the archive places
        WHEN:  the observations are settled
        THEN:  that is the refusal, distinct from a disagreement

        The two are different problems: one wants a person, the other
        wants a look at the data.
        """
        position, _, refusal = settle([])

        check.is_none(position)
        check.equal(refusal, NOTHING_PLACES_IT)

    ####################################################################
    #
    def test_the_threshold_can_be_moved(self) -> None:
        """
        GIVEN: two positions further apart than a tightened threshold
        WHEN:  the observations are settled against it
        THEN:  they are refused

        The default is chosen rather than measured, so it has to be
        movable by whoever has the data to know better.
        """
        loose, _, _ = settle([SFO, SFO_CENTROID], DISAGREE_KM)
        tight, _, refusal = settle([SFO, SFO_CENTROID], 0.1)

        check.is_not_none(loose, "agrees at the default")
        check.is_none(tight, "and not at a tighter one")
        check.is_in(SEGMENTS_DISAGREE, refusal)


########################################################################
########################################################################
#
class TestInferences:
    """Tests for working an archive out from itself."""

    ####################################################################
    #
    def test_positions_are_gathered_by_code_across_trips(
        self, two_trips: Callable[..., Archive], archive: Archive
    ) -> None:
        """
        GIVEN: an archive where one trip placed an airport
        WHEN:  its positions are gathered by key
        THEN:  the code names that position

        Gathered across every trip rather than one: the airport this
        trip left unplaced was very likely placed on another.
        """
        two_trips()

        known = positions_by_key(archive, archive.trip_keys())

        check.is_in("NAR", known)
        check.equal(known["NAR"], [NRT])
        check.is_not_in("VAN", known, "nothing placed it")

    ####################################################################
    #
    def test_an_unplaced_code_takes_the_position_from_elsewhere(
        self, two_trips: Callable[..., Archive], archive: Archive
    ) -> None:
        """
        GIVEN: one trip that placed NAR and one that did not
        WHEN:  the archive is asked what it can work out
        THEN:  the unplaced NAR is answered with the placed position
        """
        two_trips()

        found = inferences(archive, archive.trip_keys())

        answered = [i for i in found if i.answered]
        check.equal(len(answered), 1)
        check.equal(answered[0].key, "NAR")
        check.equal(answered[0].position, NRT)

    ####################################################################
    #
    def test_a_code_nothing_places_is_refused(
        self, two_trips: Callable[..., Archive], archive: Archive
    ) -> None:
        """
        GIVEN: an unplaced code no other segment places
        WHEN:  the archive is asked what it can work out
        THEN:  it is refused, and says why

        Self-healing only reaches what the archive already knows. The
        rest is what `backfill report` is for.
        """
        two_trips()

        found = inferences(archive, archive.trip_keys())

        refused = [i for i in found if not i.answered]
        check.equal(len(refused), 1)
        check.equal(refused[0].key, "VAN")
        check.equal(refused[0].refusal, NOTHING_PLACES_IT)

    ####################################################################
    #
    def test_free_text_places_are_left_out_entirely(
        self, staged_trip_of: Callable[..., str], archive: Archive
    ) -> None:
        """
        GIVEN: a trip whose unplaced endpoints are named in free text
        WHEN:  the archive is asked what it can work out
        THEN:  there is nothing to say about them

        Not refused -- absent.  A refusal implies something was
        considered and rejected, and a station name was never a
        candidate.
        """
        staged_trip_of(b.rail())

        found = inferences(archive, archive.trip_keys())

        check.equal(found, [])

    ####################################################################
    #
    def test_applying_what_was_inferred_closes_the_gap(
        self, two_trips: Callable[..., Archive], archive: Archive
    ) -> None:
        """
        GIVEN: an archive with one code it can work out
        WHEN:  what it inferred is applied
        THEN:  that gap closes and the one it could not remains

        The rows go through the same `apply_rows` a hand-edited
        work-list does, which is what makes the two safe to run in
        either order.
        """
        two_trips()
        found = inferences(archive, archive.trip_keys())

        outcome = apply_rows(archive, filled_rows(archive, found), write=True)

        check.equal(outcome.written, 1)
        left = [g for k in archive.trip_keys() for g in gaps(archive, k)]
        check.equal(len(left), 1)
        check.equal(left[0].label, "VAN")

    ####################################################################
    #
    def test_a_second_run_changes_nothing(
        self, two_trips: Callable[..., Archive], archive: Archive
    ) -> None:
        """
        GIVEN: an archive already worked out once
        WHEN:  it is worked out again
        THEN:  nothing further is written

        The gap is closed, so there is nothing left to infer -- and had
        the gap somehow persisted, the diff in `apply_rows` would still
        refuse to rewrite an unchanged value.
        """
        two_trips()
        first = inferences(archive, archive.trip_keys())
        apply_rows(archive, filled_rows(archive, first), write=True)

        again = inferences(archive, archive.trip_keys())
        outcome = apply_rows(archive, filled_rows(archive, again), write=True)

        check.equal(outcome.written, 0)

    ####################################################################
    #
    def test_an_addressed_endpoint_is_never_given_a_position(
        self, staged_trip_of: Callable[..., str], archive: Archive
    ) -> None:
        """
        GIVEN: a trip whose flights place and address both their ends
        WHEN:  the archive is asked what it can work out
        THEN:  there is nothing to do

        The address guard, which lives in what `backfill` reports rather
        than here: Tripsy geocodes an address exactly and prefers a
        stored position over doing so, so handing an addressed endpoint
        a borrowed coordinate replaces a precise pin with an approximate
        one.
        """
        staged_trip_of(b.flight(), b.lodging())

        found = inferences(archive, archive.trip_keys())

        check.equal(found, [])
