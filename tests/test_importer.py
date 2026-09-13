#!/usr/bin/env python
#
"""
Test writing staged trips into Tripsy.

The import is the one irreversible step in the project -- an identifier
Tripsy has seen is never released -- so what is asserted here is mostly
that a second run does nothing, and that the plan a person reads before
the first run matches what the first run actually sends.
"""

# system imports
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check

# Project imports
from tests import tripit_builder as b
from tripsy_exim.api import TripsyClient
from tripsy_exim.store import Archive
from tripsy_exim.sync import stage_export
from tripsy_exim.sync.importer import (
    child_ids_by_identifier,
    numbered,
    plan_trip,
    staged_children,
    staged_trip,
    upload_trip,
)


####################################################################
#
@pytest.fixture
def staged(archive: Archive) -> Archive:
    """An archive holding one trip of mixed objects, out of time order."""
    stage_export(
        archive,
        b.export(
            b.trip(
                name="Osaka, Japan, May 2024",
                objects=[
                    b.restaurant(),
                    b.flight(),
                    b.lodging(),
                    b.rail(),
                    b.ferry(),
                ],
            )
        ),
    )
    return archive


####################################################################
#
def only_key(archive: Archive) -> str:
    """The single trip key in an archive staged by the fixture."""
    keys = archive.trip_keys()
    assert len(keys) == 1
    return keys[0]


########################################################################
########################################################################
#
class TestPlan:
    """Tests for working out what a run would do, without doing it."""

    ####################################################################
    #
    def test_a_trip_is_numbered_chronologically_across_collections(
        self, staged: Archive
    ) -> None:
        """
        GIVEN: a trip whose objects span all three collections
        WHEN:  it is planned
        THEN:  sort_order is one dense sequence in time order

        The app renders a trip in this sequence and the server computes
        nothing, so a per-collection numbering would group a trip by
        object type instead of by day.
        """
        plan = plan_trip(staged, only_key(staged))

        orders = [o.sort_order for o in plan.objects]
        check.equal(orders, list(range(1, len(plan.objects) + 1)), "dense")

        instants = [
            o.starts_at for o in plan.objects if o.starts_at is not None
        ]
        check.equal(instants, sorted(instants), "in time order")
        check.greater(
            len({o.collection for o in plan.objects}), 1, "collections mixed"
        )

    ####################################################################
    #
    def test_undated_objects_sort_to_the_end(self, archive: Archive) -> None:
        """
        GIVEN: a trip holding an object with no instant
        WHEN:  it is planned
        THEN:  that object is numbered after every dated one

        Which is where the app puts its own undated objects.
        """
        stage_export(
            archive,
            b.export(b.trip(objects=[b.flight(), b.activity(day=None)])),
        )
        plan = plan_trip(archive, only_key(archive))

        undated = plan.undated
        assert undated, "the fixture must carry an object with no instant"
        check.equal(
            {o.sort_order for o in undated},
            set(range(plan.total - len(undated) + 1, plan.total + 1)),
            "undated occupy the last positions",
        )

    ####################################################################
    #
    def test_numbering_one_trip_twice_gives_one_answer(
        self, staged: Archive
    ) -> None:
        """
        GIVEN: one staged trip
        WHEN:  it is numbered twice
        THEN:  both runs agree

        A re-run resolves to the object that is already there rather than
        updating it, so a number that drifted between runs could never be
        corrected by running again.
        """
        objects = list(staged_children(staged, only_key(staged)))

        first = [(n, o.internal_identifier) for n, o in numbered(objects)]
        second = [
            (n, o.internal_identifier) for n, o in numbered(objects[::-1])
        ]

        assert first == second

    ####################################################################
    #
    def test_untyped_transportations_are_surfaced(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a trip carrying a leg the reader could not type
        WHEN:  it is planned
        THEN:  the plan names it, so it can be seen before it is written
        """
        stage_export(
            archive,
            b.export(b.trip(objects=[b.flight(), b.untyped_transport()])),
        )
        plan = plan_trip(archive, only_key(archive))

        check.equal(len(plan.untyped), 1, "one untyped leg")
        check.equal(plan.untyped[0].collection, "transportations")

    ####################################################################
    #
    def test_planning_an_unstaged_trip_is_refused(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a key no trip was staged under
        WHEN:  it is planned
        THEN:  ValueError says so rather than an empty plan coming back
        """
        with pytest.raises(ValueError, match="no trip record"):
            plan_trip(archive, "txim-nothing-g01-deadbeef")


########################################################################
########################################################################
#
class TestImport:
    """Tests for actually writing a trip."""

    ####################################################################
    #
    def test_a_trip_and_its_children_are_created(
        self, staged: Archive, api_client: TripsyClient, paced_tripsy: Any
    ) -> None:
        """
        GIVEN: one staged trip
        WHEN:  it is imported
        THEN:  the trip and every child object are created
        """
        key = only_key(staged)
        plan = plan_trip(staged, key)

        result = upload_trip(api_client, staged, key)

        check.is_true(result.trip_created, "trip created")
        check.is_not_none(result.trip_id, "and its id resolved")
        check.equal(result.created, plan.total, "every child created")
        check.equal(result.existing, 0, "nothing was already there")
        check.equal(result.failed, [], "and nothing failed")

    ####################################################################
    #
    def test_every_object_carries_its_planned_sort_order(
        self, staged: Archive, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: one staged trip
        WHEN:  it is imported
        THEN:  the values Tripsy holds are the ones the plan showed

        An import that omitted the field would leave every object on 0,
        which is what the API does when it is absent.
        """
        key = only_key(staged)
        plan = plan_trip(staged, key)

        result = upload_trip(api_client, staged, key)
        assert result.trip_id is not None

        held: dict[str, int] = {}
        for collection in ("transportations", "hostings", "activities"):
            for obj in api_client.iter_children(result.trip_id, collection):
                held[str(obj["internal_identifier"])] = obj["sort_order"]

        expected = {o.identifier: o.sort_order for o in plan.objects}
        check.equal(held, expected, "sort_order round-trips")
        check.is_not_in(0, held.values(), "nothing left on the default")

    ####################################################################
    #
    def test_a_second_run_creates_nothing(
        self, staged: Archive, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a trip that has already been imported
        WHEN:  the same import is run again
        THEN:  nothing is created and no duplicate appears

        This is the whole reason identifiers are derived from the source
        rather than minted per run.
        """
        key = only_key(staged)
        first = upload_trip(api_client, staged, key)

        second = upload_trip(api_client, staged, key)

        check.equal(second.trip_id, first.trip_id, "the same trip")
        check.is_false(second.trip_created, "which was not created again")
        check.equal(second.created, 0, "no child created")
        check.equal(second.existing, first.created, "all resolved as present")

        assert second.trip_id is not None
        counted = sum(
            len(list(api_client.iter_children(second.trip_id, collection)))
            for collection in ("transportations", "hostings", "activities")
        )
        check.equal(counted, first.created, "and no duplicates on the trip")

    ####################################################################
    #
    def test_a_run_resumes_after_a_partial_one(
        self, staged: Archive, api_client: TripsyClient, paced_tripsy: Any
    ) -> None:
        """
        GIVEN: a trip whose children were only partly written
        WHEN:  the import is run again
        THEN:  the missing ones are created and the rest left alone
        """
        key = only_key(staged)
        plan = plan_trip(staged, key)

        # Write the trip and one child by hand, as a failed run would
        # have left it.
        trip = staged_trip(staged, key)
        assert trip is not None
        _, created = paced_tripsy.create_trip(trip.writable_payload())
        trip_id = created["id"]
        first = plan.objects[0]
        paced_tripsy.create_child(
            trip_id,
            first.collection,
            {"internal_identifier": first.identifier, "sort_order": 999},
        )

        result = upload_trip(api_client, staged, key)

        check.is_false(result.trip_created, "the trip was already there")
        check.equal(result.existing, 1, "the one child already written")
        check.equal(result.created, plan.total - 1, "the rest created")

        # The number an object got on its first create is the number it
        # keeps: the second create is suppressed rather than applied, so
        # a resumed run leaves the earlier value in place.  Repairing one
        # is a PATCH, which is why the plan is read before the run.
        held = {
            obj["internal_identifier"]: obj["sort_order"]
            for obj in api_client.iter_children(trip_id, first.collection)
        }
        check.equal(held[first.identifier], 999, "the partial run's value")
        for obj in plan.objects:
            if obj.collection == first.collection and obj is not first:
                check.equal(
                    held[obj.identifier], obj.sort_order, "the planned value"
                )

    ####################################################################
    #
    def test_children_resolve_back_to_their_ids(
        self, staged: Archive, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: an imported trip
        WHEN:  a collection is resolved by identifier
        THEN:  every object in it is found

        A duplicate create answers an empty 200 with no id, so this is
        the only way to reach an object that is already there.
        """
        key = only_key(staged)
        result = upload_trip(api_client, staged, key)
        assert result.trip_id is not None

        found = child_ids_by_identifier(
            api_client, result.trip_id, "transportations"
        )

        plan = plan_trip(staged, key)
        wanted = {
            o.identifier
            for o in plan.objects
            if o.collection == "transportations"
        }
        check.equal(set(found), wanted, "every transportation resolved")

    ####################################################################
    #
    def test_importing_an_unstaged_trip_is_refused(
        self, archive: Archive, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a key no trip was staged under
        WHEN:  it is imported
        THEN:  ValueError is raised before anything is sent
        """
        with pytest.raises(ValueError, match="no trip record"):
            upload_trip(api_client, archive, "txim-nothing-g01-deadbeef")
