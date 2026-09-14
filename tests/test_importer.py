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
import json
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check

# Project imports
from tests import tripit_builder as b
from tripsy_exim.api import TripsyClient
from tripsy_exim.models import namespace_of
from tripsy_exim.store import Archive
from tripsy_exim.sync import REPORT_FILENAME, stage_export
from tripsy_exim.sync.importer import (
    added,
    child_ids_by_identifier,
    corrections,
    declare_merge,
    numbered,
    plan_trip,
    spanning,
    staged_children,
    staged_trip,
    undo_merge,
    upload_trip,
)
from tripsy_exim.sync.overrides import OverrideSet, save_overrides


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


########################################################################
########################################################################
#
class TestMerge:
    """Tests for uploading one staged trip as part of another."""

    ####################################################################
    #
    @pytest.fixture
    def pair(self, archive: Archive) -> tuple[str, str]:
        """
        Two trips of one journey, as the export records them.

        One traveller flew from one airport and one from another, each
        with their own room, and one of the two records the itinerary
        they shared.
        """
        stage_export(
            archive,
            b.export(
                b.trip(
                    name="Honolulu, HI, November 2011",
                    start="2011-11-09",
                    end="2011-11-14",
                    objects=[b.flight(), b.lodging()],
                ),
                b.trip(
                    name="Honolulu, HI, November 2011",
                    start="2011-11-09",
                    end="2011-11-14",
                    objects=[b.rail(), b.lodging(), b.restaurant()],
                ),
            ),
        )
        keys = archive.trip_keys()
        assert len(keys) == 2
        smaller, larger = sorted(
            keys, key=lambda k: len(list(staged_children(archive, k)))
        )
        return smaller, larger

    ####################################################################
    #
    def test_a_merged_trip_carries_both_halves(
        self, archive: Archive, pair: tuple[str, str]
    ) -> None:
        """
        GIVEN: two staged trips of one journey
        WHEN:  one is declared as part of the other
        THEN:  the target plans both halves as one trip

        Uploading both would make two rival trips out of one journey, and
        neither half is redundant: each holds that traveller's own
        flights and room.
        """
        smaller, larger = pair
        before = plan_trip(archive, larger).total
        added = len(list(staged_children(archive, smaller)))

        declare_merge(archive, smaller, larger)

        plan = plan_trip(archive, larger)
        check.equal(plan.total + plan.duplicates, before + added, "all of it")
        check.greater(plan.total, before, "and more than either half alone")

    ####################################################################
    #
    def test_a_merged_trip_is_numbered_as_one_journey(
        self, archive: Archive, pair: tuple[str, str]
    ) -> None:
        """
        GIVEN: a merged pair
        WHEN:  the target is planned
        THEN:  sort_order is one dense sequence over both halves

        Numbering each half separately would give two objects the same
        position and read as one traveller's trip followed by the other's.
        """
        smaller, larger = pair
        declare_merge(archive, smaller, larger)

        plan = plan_trip(archive, larger)

        orders = [o.sort_order for o in plan.objects]
        check.equal(orders, list(range(1, plan.total + 1)), "dense")
        instants = [o.starts_at for o in plan.objects if o.starts_at]
        check.equal(instants, sorted(instants), "and in time order")

    ####################################################################
    #
    @pytest.mark.parametrize(
        "wrong",
        ["itself", "unstaged"],
    )
    def test_a_merge_that_makes_no_sense_is_refused(
        self, archive: Archive, pair: tuple[str, str], wrong: str
    ) -> None:
        """
        GIVEN: a merge naming one trip twice, or a trip that is not there
        WHEN:  it is declared
        THEN:  ValueError says so
        """
        smaller, larger = pair
        absorbed = larger if wrong == "itself" else "txim-nothing-g01-dead"

        with pytest.raises(ValueError):
            declare_merge(archive, absorbed, larger)

    ####################################################################
    #
    def test_merging_into_an_absorbed_trip_is_refused(
        self, archive: Archive, pair: tuple[str, str]
    ) -> None:
        """
        GIVEN: a trip already declared as part of another
        WHEN:  a third is declared as part of *it*
        THEN:  it is refused, naming where the objects actually go

        A chain would silently drop the middle trip's objects, since the
        uploader gathers one level.
        """
        smaller, larger = pair
        declare_merge(archive, smaller, larger)

        with pytest.raises(ValueError, match="itself merged into"):
            declare_merge(archive, larger, smaller)

    ####################################################################
    #
    def test_a_flight_is_labelled_by_its_ends_in_the_plan(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a flight, which carries no name
        WHEN:  the trip is planned
        THEN:  the plan calls it by its endpoints

        Which is what the app will call it, and a blank row tells a
        reader nothing before the one irreversible step.
        """
        stage_export(archive, b.export(b.trip(objects=[b.flight()])))
        plan = plan_trip(archive, archive.trip_keys()[0])

        check.equal(plan.objects[0].name, "SAN to OSA")

    ####################################################################
    #
    def test_an_object_the_target_already_has_is_not_sent_twice(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: two trips of one journey that share a flight
        WHEN:  one is merged into the other
        THEN:  the shared flight is planned once, and counted as skipped

        Two travellers on one flight each carry that flight in their own
        record.  Identifiers cannot catch it: they are derived per trip,
        so the same flight in two records mints two of them and both
        would be created.
        """
        shared = b.flight()
        stage_export(
            archive,
            b.export(
                b.trip(
                    name="Burlington, VT, September 2010",
                    objects=[shared, b.restaurant()],
                ),
                b.trip(
                    name="Burlington, VT, September 2010",
                    objects=[shared],
                ),
            ),
        )
        keys = archive.trip_keys()
        smaller, larger = sorted(
            keys, key=lambda k: len(list(staged_children(archive, k)))
        )

        declare_merge(archive, smaller, larger)
        plan = plan_trip(archive, larger)

        check.equal(plan.duplicates, 1, "the shared flight was skipped")
        flights = [o for o in plan.objects if o.collection == "transportations"]
        check.equal(len(flights), 1, "and planned once")

    ####################################################################
    #
    def test_a_merged_trip_spans_what_it_absorbs(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a trip absorbing one that runs outside its own dates
        WHEN:  the trip record is prepared
        THEN:  its dates widen to cover both

        The record doing the absorbing need not be the one that ran
        longest -- a weekend reached the archive as a hotel booking and a
        day trip, and the day is the record with more objects.  Left
        alone the trip would say it lasted an afternoon.
        """
        stage_export(
            archive,
            b.export(
                b.trip(
                    name="San Francisco, CA, July 2012",
                    start="2012-07-06",
                    end="2012-07-08",
                    objects=[b.lodging()],
                ),
                b.trip(
                    name="Angel Island",
                    start="2012-07-07",
                    end="2012-07-07",
                    objects=[b.activity(), b.restaurant()],
                ),
            ),
        )
        keys = archive.trip_keys()
        day = next(
            k
            for k in keys
            if str(getattr(staged_trip(archive, k), "name", ""))
            == "Angel Island"
        )
        weekend = next(k for k in keys if k != day)

        declare_merge(archive, weekend, day)
        record = staged_trip(archive, day)
        assert record is not None
        widened = spanning(archive, record, day)

        check.equal(str(widened.starts_at), "2012-07-06", "widened back")
        check.equal(str(widened.ends_at), "2012-07-08", "and forward")

    ####################################################################
    #
    def test_a_merge_can_be_undone(
        self, archive: Archive, pair: tuple[str, str]
    ) -> None:
        """
        GIVEN: a declared merge
        WHEN:  it is undone
        THEN:  the trip plans on its own again

        Which direction a merge runs decides the surviving trip's name
        and dates, so getting it the wrong way round has to be fixable.
        """
        smaller, larger = pair
        declare_merge(archive, smaller, larger)
        merged = plan_trip(archive, larger).total

        undo_merge(archive, smaller)

        check.less(plan_trip(archive, larger).total, merged)
        with pytest.raises(ValueError, match="not merged"):
            undo_merge(archive, smaller)


########################################################################
########################################################################
#
class TestCorrections:
    """Tests for laying corrections over what is uploaded."""

    ####################################################################
    #
    def test_a_correction_reaches_what_is_sent(self, archive: Archive) -> None:
        """
        GIVEN: a staged trip and a correction against one of its objects
        WHEN:  the trip is planned
        THEN:  the corrected value is what would be sent

        The archive is meant to be the good copy, so a fact the export
        never carried -- the address of a ferry pier -- belongs in it
        rather than being repaired in Tripsy afterwards.
        """
        stage_export(archive, b.export(b.trip(objects=[b.ferry()])))
        trip_key = archive.trip_keys()[0]
        report = json.loads(
            (archive.trip_dir(trip_key) / REPORT_FILENAME).read_text()
        )
        uuid = next(
            u
            for u, e in report["index"].items()
            if e["collection"] == "transportations"
        )

        overrides = OverrideSet(trip_uuid=report["trip_uuid"])
        overrides.correct(
            uuid,
            departure_address="Sakurajima Port, Kagoshima, Japan",
            name="Sakurajima Port to Kagoshima Port",
        )
        save_overrides(archive, overrides)

        plan = plan_trip(archive, trip_key)
        leg = next(o for o in plan.objects if o.collection == "transportations")
        check.equal(leg.name, "Sakurajima Port to Kagoshima Port")

    ####################################################################
    #
    def test_a_trip_with_no_corrections_is_unchanged(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a staged trip nobody has corrected
        WHEN:  the trip is planned
        THEN:  nothing about it differs

        Most trips carry no correction at all, so the common path has to
        cost nothing and change nothing.
        """
        stage_export(archive, b.export(b.trip(objects=[b.ferry()])))
        trip_key = archive.trip_keys()[0]

        check.equal(corrections(archive, trip_key), {})
        leg = next(
            o
            for o in plan_trip(archive, trip_key).objects
            if o.collection == "transportations"
        )
        check.equal(leg.name, "Example Ferry", "the carrier, as parsed")

    ####################################################################
    #
    def test_a_correction_survives_a_change_of_namespace(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a correction made against a trip staged one way
        WHEN:  the same export is staged into a scratch namespace
        THEN:  the correction still reaches the object

        Which is the whole reason corrections are keyed by the source
        uuid: a shaping run mints different identifiers from the same
        uuids, and a correction made during one has to apply to the real
        run too.
        """
        document = b.export(b.trip(objects=[b.ferry()]))
        stage_export(archive, document)
        first = archive.trip_keys()[0]
        report = json.loads(
            (archive.trip_dir(first) / REPORT_FILENAME).read_text()
        )
        uuid = next(iter(report["index"]))
        overrides = OverrideSet(trip_uuid=report["trip_uuid"])
        overrides.correct(uuid, name="corrected")
        save_overrides(archive, overrides)

        stage_export(archive, document, "scratch-abcd1234")
        scratch = next(k for k in archive.trip_keys() if "scratch" in k)

        found = corrections(archive, scratch)
        check.equal(len(found), 1, "reached the scratch copy too")
        check.is_in("corrected", [o.fields.get("name") for o in found.values()])


########################################################################
########################################################################
#
class TestAdditions:
    """Tests for uploading an object no source record held."""

    ####################################################################
    #
    def added_trip(self, archive: Archive, **fields: Any) -> str:
        """Stage one ferry trip and add an object to it.  Returns its key."""
        stage_export(archive, b.export(b.trip(objects=[b.ferry()])))
        trip_key = archive.trip_keys()[0]
        report = json.loads(
            (archive.trip_dir(trip_key) / REPORT_FILENAME).read_text()
        )
        overrides = OverrideSet(trip_uuid=report["trip_uuid"])
        overrides.add("a1", "transportations", **fields)
        save_overrides(archive, overrides)
        return trip_key

    ####################################################################
    #
    def test_an_addition_is_uploaded_with_the_trip(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a staged trip and an object added to it by hand
        WHEN:  the trip is planned
        THEN:  the addition is among what would be sent

        The shuttle from the terminal to the rental counter is not in any
        booking, because all anyone had to do was find the bus.
        """
        trip_key = self.added_trip(
            archive,
            name="LAX to the rental counter",
            transportation_type="transfer",
            departure_at="2024-05-03T08:00:00Z",
        )

        plan = plan_trip(archive, trip_key)
        names = [o.name for o in plan.objects]
        check.is_in("LAX to the rental counter", names)
        check.equal(len(plan.objects), 2, "the ferry and the addition")

    ####################################################################
    #
    def test_an_addition_is_numbered_by_when_it_happened(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: an addition timed before the trip's only parsed object
        WHEN:  the trip is planned
        THEN:  it takes the first `sort_order`

        An addition is numbered with everything else, or it would land at
        the end of the trip with the objects carrying no time at all.
        """
        trip_key = self.added_trip(
            archive,
            name="the shuttle",
            transportation_type="transfer",
            departure_at="2024-05-02T23:00:00Z",
        )

        plan = plan_trip(archive, trip_key)
        check.equal(plan.objects[0].name, "the shuttle")
        check.equal(plan.objects[0].sort_order, 1)

    ####################################################################
    #
    def test_an_addition_is_minted_into_the_trip_s_namespace(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: an addition to a trip staged under the parser's namespace
        WHEN:  its identifier is minted
        THEN:  it carries that namespace and is stable across reads

        An archive is self-describing: nothing has to tell an addition
        which run it belongs to.
        """
        trip_key = self.added_trip(archive, name="the shuttle")

        trip = staged_trip(archive, trip_key)
        assert trip is not None
        once = added(archive, trip_key)[0]
        twice = added(archive, trip_key)[0]

        check.equal(once.internal_identifier, twice.internal_identifier)
        check.equal(
            namespace_of(once.internal_identifier),
            namespace_of(trip.internal_identifier),
        )

    ####################################################################
    #
    def test_an_addition_survives_re_staging(self, archive: Archive) -> None:
        """
        GIVEN: an addition to a trip whose export is staged again
        WHEN:  the trip is planned
        THEN:  the addition is still there

        Staging prunes whatever the parser no longer produces, and it
        never produces an addition.  Living outside the trip directory is
        what keeps one.
        """
        document = b.export(b.trip(objects=[b.ferry()]))
        stage_export(archive, document)
        trip_key = archive.trip_keys()[0]
        report = json.loads(
            (archive.trip_dir(trip_key) / REPORT_FILENAME).read_text()
        )
        overrides = OverrideSet(trip_uuid=report["trip_uuid"])
        overrides.add("a1", "transportations", name="the shuttle")
        save_overrides(archive, overrides)

        stage_export(archive, document)

        names = [o.name for o in plan_trip(archive, trip_key).objects]
        check.is_in("the shuttle", names)

    ####################################################################
    #
    def test_a_trip_with_no_additions_gains_nothing(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a staged trip nobody has added to
        WHEN:  its additions are read
        THEN:  none come back

        Almost every trip is this one, so the common path has to cost
        nothing.
        """
        stage_export(archive, b.export(b.trip(objects=[b.ferry()])))

        check.equal(added(archive, archive.trip_keys()[0]), [])


########################################################################
