#!/usr/bin/env python
#
"""
Test writing staged trips into Tripsy.

The import is the one irreversible step in the project -- an identifier
Tripsy has seen is never released -- so what is asserted here is mostly
that a second run does nothing, and that the plan a person reads before
the first run matches what the first run actually sends.

Setup lives in fixtures rather than in the tests.  What a trip has to
look like before a behaviour can be provoked is often longer than the
behaviour itself, and a test that opens with twenty lines of staging
reads as though the staging were the point.  Each fixture's docstring
says what it builds, so a test can name it and get on with asserting.
"""

# system imports
import json
from dataclasses import dataclass, field
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
    TripImport,
    added,
    child_ids_by_identifier,
    composed_children,
    corrections,
    declare_merge,
    numbered,
    plan_trip,
    positions_in,
    spanning,
    staged_children,
    staged_trip,
    undo_merge,
    unplaced_in,
    upload_trip,
    verify_trip,
)
from tripsy_exim.sync.overrides import OverrideSet, save_overrides


####################################################################
#
def only_key(archive: Archive) -> str:
    """The single trip key in an archive holding exactly one trip."""
    keys = archive.trip_keys()
    assert len(keys) == 1
    return keys[0]


####################################################################
#
def by_size(archive: Archive) -> tuple[str, str]:
    """
    The two trip keys in an archive, fewest objects first.

    Which of a pair absorbs the other is decided by object count, so the
    two halves of a merge are named this way rather than by key order,
    which is a digest and means nothing.
    """
    keys = archive.trip_keys()
    assert len(keys) == 2
    smaller, larger = sorted(
        keys, key=lambda k: len(list(staged_children(archive, k)))
    )
    return smaller, larger


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
@pytest.fixture
def staged_key(staged: Archive) -> str:
    """The key of the one trip `staged` holds."""
    return only_key(staged)


####################################################################
#
@pytest.fixture
def uploaded(
    staged: Archive, staged_key: str, api_client: TripsyClient
) -> TripImport:
    """
    The `staged` trip, uploaded, with its Tripsy id resolved.

    Reading a trip back needs one that is there, and every test that
    does so wants the same trip in the same state.  The id is asserted
    here so no test has to repeat the check before using it.
    """
    result = upload_trip(api_client, staged, staged_key)
    assert result.trip_id is not None
    return result


########################################################################
########################################################################
#
@dataclass
class FerryTrip:
    """
    One staged trip holding a single ferry, and the handles to correct it.

    Corrections are keyed by the source record's uuid, which lives in the
    reader's report rather than on the object, so reaching one means
    staging the trip, reading `report.json`, and picking the entry out of
    its index.  That is four lines of lookup before a test can say what
    it actually wants to correct, which is what this carries.
    """

    archive: Archive
    key: str
    trip_uuid: str
    document: dict[str, Any]
    index: dict[str, dict[str, str]]
    overrides: OverrideSet = field(init=False)

    ####################################################################
    #
    def __post_init__(self) -> None:
        self.overrides = OverrideSet(trip_uuid=self.trip_uuid)

    ####################################################################
    #
    @property
    def leg_uuid(self) -> str:
        """The source uuid of the trip's one transportation."""
        return next(
            uuid
            for uuid, entry in self.index.items()
            if entry["collection"] == "transportations"
        )

    ####################################################################
    #
    def correct(self, **fields: Any) -> None:
        """Correct the trip's ferry, and save."""
        self.overrides.correct(self.leg_uuid, **fields)
        save_overrides(self.archive, self.overrides)

    ####################################################################
    #
    def add(self, collection: str = "transportations", **fields: Any) -> None:
        """Add an object no source record held, and save."""
        self.overrides.add("a1", collection, **fields)
        save_overrides(self.archive, self.overrides)

    ####################################################################
    #
    def stage_again(self, namespace: str | None = None) -> None:
        """Stage the same export again, optionally into another namespace."""
        if namespace is None:
            stage_export(self.archive, self.document)
        else:
            stage_export(self.archive, self.document, namespace)


####################################################################
#
@pytest.fixture
def ferry_trip(archive: Archive) -> FerryTrip:
    """
    One staged trip carrying a single ferry, ready to be corrected.

    A ferry because the export gives one no address and a name taken
    from the carrier, so it is the object a correction is actually for.
    See `FerryTrip` for what comes with it.
    """
    document = b.export(b.trip(objects=[b.ferry()]))
    stage_export(archive, document)
    key = only_key(archive)
    report = json.loads((archive.trip_dir(key) / REPORT_FILENAME).read_text())
    return FerryTrip(
        archive=archive,
        key=key,
        trip_uuid=report["trip_uuid"],
        document=document,
        index=report["index"],
    )


########################################################################
########################################################################
#
class TestPlan:
    """Tests for working out what a run would do, without doing it."""

    ####################################################################
    #
    def test_a_trip_is_numbered_chronologically_across_collections(
        self, staged: Archive, staged_key: str
    ) -> None:
        """
        GIVEN: a trip whose objects span all three collections
        WHEN:  it is planned
        THEN:  sort_order is one dense sequence in time order

        The app renders a trip in this sequence and the server computes
        nothing, so a per-collection numbering would group a trip by
        object type instead of by day.
        """
        plan = plan_trip(staged, staged_key)

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
        self, staged: Archive, staged_key: str
    ) -> None:
        """
        GIVEN: one staged trip
        WHEN:  it is numbered twice
        THEN:  both runs agree

        A re-run resolves to the object that is already there rather than
        updating it, so a number that drifted between runs could never be
        corrected by running again.
        """
        objects = list(staged_children(staged, staged_key))

        first = [(n, o.internal_identifier) for n, o in numbered(objects)]
        second = [
            (n, o.internal_identifier) for n, o in numbered(objects[::-1])
        ]

        assert first == second

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
        plan = plan_trip(archive, only_key(archive))

        check.equal(plan.objects[0].name, "SAN to OSA")

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
    @pytest.fixture
    def partly_written(
        self,
        staged: Archive,
        staged_key: str,
        paced_tripsy: Any,
    ) -> tuple[int, Any]:
        """
        A trip left as a run that died part way through would leave it.

        The trip record and exactly one of its children are written by
        hand, the child carrying a sort_order no plan would ever give it
        so that a resumed run can be seen to leave it alone.

        Returns the trip's Tripsy id and the planned object that was
        written.
        """
        trip = staged_trip(staged, staged_key)
        assert trip is not None
        _, created = paced_tripsy.create_trip(trip.writable_payload())
        trip_id = created["id"]

        first = plan_trip(staged, staged_key).objects[0]
        paced_tripsy.create_child(
            trip_id,
            first.collection,
            {"internal_identifier": first.identifier, "sort_order": 999},
        )
        return trip_id, first

    ####################################################################
    #
    def test_a_trip_and_its_children_are_created(
        self,
        staged: Archive,
        staged_key: str,
        api_client: TripsyClient,
        paced_tripsy: Any,
    ) -> None:
        """
        GIVEN: one staged trip
        WHEN:  it is imported
        THEN:  the trip and every child object are created
        """
        plan = plan_trip(staged, staged_key)

        result = upload_trip(api_client, staged, staged_key)

        check.is_true(result.trip_created, "trip created")
        check.is_not_none(result.trip_id, "and its id resolved")
        check.equal(result.created, plan.total, "every child created")
        check.equal(result.existing, 0, "nothing was already there")
        check.equal(result.failed, [], "and nothing failed")

    ####################################################################
    #
    def test_every_object_carries_its_planned_sort_order(
        self,
        staged: Archive,
        staged_key: str,
        api_client: TripsyClient,
        uploaded: TripImport,
    ) -> None:
        """
        GIVEN: one staged trip
        WHEN:  it is imported
        THEN:  the values Tripsy holds are the ones the plan showed

        An import that omitted the field would leave every object on 0,
        which is what the API does when it is absent.
        """
        plan = plan_trip(staged, staged_key)
        assert uploaded.trip_id is not None

        held: dict[str, int] = {}
        for collection in ("transportations", "hostings", "activities"):
            for obj in api_client.iter_children(uploaded.trip_id, collection):
                held[str(obj["internal_identifier"])] = obj["sort_order"]

        expected = {o.identifier: o.sort_order for o in plan.objects}
        check.equal(held, expected, "sort_order round-trips")
        check.is_not_in(0, held.values(), "nothing left on the default")

    ####################################################################
    #
    def test_a_second_run_creates_nothing(
        self,
        staged: Archive,
        staged_key: str,
        api_client: TripsyClient,
        uploaded: TripImport,
    ) -> None:
        """
        GIVEN: a trip that has already been imported
        WHEN:  the same import is run again
        THEN:  nothing is created and no duplicate appears

        This is the whole reason identifiers are derived from the source
        rather than minted per run.
        """
        second = upload_trip(api_client, staged, staged_key)

        check.equal(second.trip_id, uploaded.trip_id, "the same trip")
        check.is_false(second.trip_created, "which was not created again")
        check.equal(second.created, 0, "no child created")
        check.equal(
            second.existing, uploaded.created, "all resolved as present"
        )

        assert second.trip_id is not None
        counted = sum(
            len(list(api_client.iter_children(second.trip_id, collection)))
            for collection in ("transportations", "hostings", "activities")
        )
        check.equal(counted, uploaded.created, "and no duplicates on the trip")

    ####################################################################
    #
    def test_a_run_resumes_after_a_partial_one(
        self,
        staged: Archive,
        staged_key: str,
        api_client: TripsyClient,
        partly_written: tuple[int, Any],
    ) -> None:
        """
        GIVEN: a trip whose children were only partly written
        WHEN:  the import is run again
        THEN:  the missing ones are created and the rest left alone
        """
        trip_id, first = partly_written
        plan = plan_trip(staged, staged_key)

        result = upload_trip(api_client, staged, staged_key)

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
            # Compared by identifier rather than identity: `first` came
            # from the fixture's own plan, and planning again builds new
            # objects for the same records.
            #
            if (
                obj.collection == first.collection
                and obj.identifier != first.identifier
            ):
                check.equal(
                    held[obj.identifier], obj.sort_order, "the planned value"
                )

    ####################################################################
    #
    def test_children_resolve_back_to_their_ids(
        self,
        staged: Archive,
        staged_key: str,
        api_client: TripsyClient,
        uploaded: TripImport,
    ) -> None:
        """
        GIVEN: an imported trip
        WHEN:  a collection is resolved by identifier
        THEN:  every object in it is found

        A duplicate create answers an empty 200 with no id, so this is
        the only way to reach an object that is already there.
        """
        assert uploaded.trip_id is not None

        found = child_ids_by_identifier(
            api_client, uploaded.trip_id, "transportations"
        )

        plan = plan_trip(staged, staged_key)
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
        they shared.  Given fewest objects first, which is the direction
        a merge runs.
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
        return by_size(archive)

    ####################################################################
    #
    @pytest.fixture
    def sharing_a_flight(self, archive: Archive) -> tuple[str, str]:
        """
        Two trips of one journey that both record the same flight.

        Two travellers on one flight each carry that flight in their own
        record, and identifiers cannot catch it: they are derived per
        trip, so one flight in two records mints two of them.  Given
        fewest objects first.
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
        return by_size(archive)

    ####################################################################
    #
    @pytest.fixture
    def weekend_and_day(self, archive: Archive) -> tuple[str, str]:
        """
        A weekend booking and a day trip inside it, given in that order.

        The record doing the absorbing need not be the one that ran
        longest: the weekend reached the archive as a hotel booking
        alone, and the day trip is the record with more objects.  Left
        alone the merged trip would say it lasted an afternoon.
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
            key
            for key in keys
            if str(getattr(staged_trip(archive, key), "name", ""))
            == "Angel Island"
        )
        weekend = next(key for key in keys if key != day)
        return weekend, day

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
        _, larger = pair
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
    def test_an_object_the_target_already_has_is_not_sent_twice(
        self, archive: Archive, sharing_a_flight: tuple[str, str]
    ) -> None:
        """
        GIVEN: two trips of one journey that share a flight
        WHEN:  one is merged into the other
        THEN:  the shared flight is planned once, and counted as skipped
        """
        smaller, larger = sharing_a_flight

        declare_merge(archive, smaller, larger)
        plan = plan_trip(archive, larger)

        check.equal(plan.duplicates, 1, "the shared flight was skipped")
        flights = [o for o in plan.objects if o.collection == "transportations"]
        check.equal(len(flights), 1, "and planned once")

    ####################################################################
    #
    def test_a_trip_holding_one_record_twice_sends_it_once(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: one trip carrying the same journey twice
        WHEN:  it is planned
        THEN:  the journey is planned once, and counted as skipped

        A source can record one train twice, and two objects alike in
        kind, instant and label are alike to a reader of the app too --
        so a second one puts a duplicate on the itinerary rather than
        recording anything the first does not.
        """
        stage_export(
            archive,
            b.export(b.trip(objects=[b.rail(), b.rail(), b.restaurant()])),
        )

        plan = plan_trip(archive, only_key(archive))

        check.equal(plan.duplicates, 1, "the second copy was skipped")
        legs = [o for o in plan.objects if o.collection == "transportations"]
        check.equal(len(legs), 1, "and planned once")

    ####################################################################
    #
    def test_a_merged_trip_spans_what_it_absorbs(
        self, archive: Archive, weekend_and_day: tuple[str, str]
    ) -> None:
        """
        GIVEN: a trip absorbing one that runs outside its own dates
        WHEN:  the trip record is prepared
        THEN:  its dates widen to cover both
        """
        weekend, day = weekend_and_day

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
    def test_a_correction_reaches_what_is_sent(
        self, ferry_trip: FerryTrip
    ) -> None:
        """
        GIVEN: a staged trip and a correction against one of its objects
        WHEN:  the trip is planned
        THEN:  the corrected value is what would be sent

        The archive is meant to be the good copy, so a fact the export
        never carried -- the address of a ferry pier -- belongs in it
        rather than being repaired in Tripsy afterwards.
        """
        ferry_trip.correct(
            departure_address="Sakurajima Port, Kagoshima, Japan",
            name="Sakurajima Port to Kagoshima Port",
        )

        plan = plan_trip(ferry_trip.archive, ferry_trip.key)

        leg = next(o for o in plan.objects if o.collection == "transportations")
        check.equal(leg.name, "Sakurajima Port to Kagoshima Port")

    ####################################################################
    #
    def test_a_trip_with_no_corrections_is_unchanged(
        self, ferry_trip: FerryTrip
    ) -> None:
        """
        GIVEN: a staged trip nobody has corrected
        WHEN:  the trip is planned
        THEN:  nothing about it differs

        Most trips carry no correction at all, so the common path has to
        cost nothing and change nothing.
        """
        check.equal(corrections(ferry_trip.archive, ferry_trip.key), {})

        leg = next(
            o
            for o in plan_trip(ferry_trip.archive, ferry_trip.key).objects
            if o.collection == "transportations"
        )
        check.equal(leg.name, "Example Ferry", "the carrier, as parsed")

    ####################################################################
    #
    def test_a_correction_survives_a_change_of_namespace(
        self, ferry_trip: FerryTrip
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
        ferry_trip.correct(name="corrected")

        ferry_trip.stage_again("scratch-abcd1234")
        scratch = next(
            key for key in ferry_trip.archive.trip_keys() if "scratch" in key
        )

        found = corrections(ferry_trip.archive, scratch)
        check.equal(len(found), 1, "reached the scratch copy too")
        check.is_in("corrected", [o.fields.get("name") for o in found.values()])


########################################################################
########################################################################
#
class TestAdditions:
    """Tests for uploading an object no source record held."""

    ####################################################################
    #
    def test_an_addition_is_uploaded_and_numbered_with_the_trip(
        self, ferry_trip: FerryTrip
    ) -> None:
        """
        GIVEN: a staged trip and an object added to it by hand, timed
               before the trip's only parsed object
        WHEN:  the trip is planned
        THEN:  the addition is sent, and numbered by when it happened

        The shuttle from the terminal to the rental counter is in no
        booking, because all anyone had to do was find the bus.  It is
        numbered with everything else, or it would land at the end of the
        trip among the objects carrying no time at all.
        """
        ferry_trip.add(
            name="the shuttle",
            transportation_type="transfer",
            departure_at="2024-05-02T23:00:00Z",
        )

        plan = plan_trip(ferry_trip.archive, ferry_trip.key)

        check.equal(len(plan.objects), 2, "the ferry and the addition")
        check.equal(plan.objects[0].name, "the shuttle")
        check.equal(plan.objects[0].sort_order, 1)

    ####################################################################
    #
    def test_an_addition_is_minted_into_the_trip_s_namespace(
        self, ferry_trip: FerryTrip
    ) -> None:
        """
        GIVEN: an addition to a trip staged under the parser's namespace
        WHEN:  its identifier is minted
        THEN:  it carries that namespace and is stable across reads

        An archive is self-describing: nothing has to tell an addition
        which run it belongs to.
        """
        ferry_trip.add(name="the shuttle")

        trip = staged_trip(ferry_trip.archive, ferry_trip.key)
        assert trip is not None
        once = added(ferry_trip.archive, ferry_trip.key)[0]
        twice = added(ferry_trip.archive, ferry_trip.key)[0]

        check.equal(once.internal_identifier, twice.internal_identifier)
        check.equal(
            namespace_of(once.internal_identifier),
            namespace_of(trip.internal_identifier),
        )

    ####################################################################
    #
    def test_an_addition_survives_re_staging(
        self, ferry_trip: FerryTrip
    ) -> None:
        """
        GIVEN: an addition to a trip whose export is staged again
        WHEN:  the trip is planned
        THEN:  the addition is still there

        Staging prunes whatever the parser no longer produces, and it
        never produces an addition.  Living outside the trip directory is
        what keeps one.
        """
        ferry_trip.add(name="the shuttle")

        ferry_trip.stage_again()

        names = [
            o.name
            for o in plan_trip(ferry_trip.archive, ferry_trip.key).objects
        ]
        check.is_in("the shuttle", names)

    ####################################################################
    #
    def test_a_trip_with_no_additions_gains_nothing(
        self, ferry_trip: FerryTrip
    ) -> None:
        """
        GIVEN: a staged trip nobody has added to
        WHEN:  its additions are read
        THEN:  none come back

        Almost every trip is this one, so the common path has to cost
        nothing.
        """
        check.equal(added(ferry_trip.archive, ferry_trip.key), [])


########################################################################
########################################################################
#
class TestComposedChildren:
    """Tests for reading a trip as it will be uploaded."""

    ####################################################################
    #
    def test_a_correction_shows_here_and_not_on_disk(
        self, ferry_trip: FerryTrip
    ) -> None:
        """
        GIVEN: a staged trip with a correction against its one object
        WHEN:  its objects are composed, and read off disk beside that
        THEN:  only the composed ones carry the correction

        Staging owns the files under a trip, so a correction is laid over
        on the way out rather than written back.  Anything asking what a
        trip still lacks has to ask the composed view, or a field an
        earlier correction filled reads as a gap again.
        """
        ferry_trip.correct(name="Sado Kisen")

        composed = composed_children(ferry_trip.archive, ferry_trip.key)
        on_disk = list(staged_children(ferry_trip.archive, ferry_trip.key))

        check.is_in("Sado Kisen", {obj.name for obj in composed})
        check.is_not_in("Sado Kisen", {obj.name for obj in on_disk})

    ####################################################################
    #
    def test_an_addition_is_composed_in(self, ferry_trip: FerryTrip) -> None:
        """
        GIVEN: a staged trip and an object added to it by hand
        WHEN:  its objects are composed
        THEN:  the addition is among them

        An addition is as much a part of the trip as a parsed record and
        carries the same gaps, but it is no file under the trip, so a
        walk of the archive misses it.
        """
        ferry_trip.add(name="the shuttle", transportation_type="transfer")

        composed = composed_children(ferry_trip.archive, ferry_trip.key)
        on_disk = list(staged_children(ferry_trip.archive, ferry_trip.key))

        check.equal(len(composed), len(on_disk) + 1)
        check.is_in("the shuttle", {obj.name for obj in composed})

    ####################################################################
    #
    def test_an_absorbed_trip_stays_its_own(self, archive: Archive) -> None:
        """
        GIVEN: one staged trip declared as absorbed by another
        WHEN:  each trip's objects are composed
        THEN:  neither gains the other's

        An absorbed trip uploads as part of its target but remains its
        own staged trip, with its own report and its own corrections, so
        a correction against it is authored against it rather than
        against the trip carrying it.
        """
        stage_export(
            archive,
            b.export(
                b.trip(name="Kyoto, May 2011", objects=[b.lodging()]),
                b.trip(
                    name="Kyoto, May 2011",
                    objects=[b.flight(), b.restaurant()],
                ),
            ),
        )
        smaller, larger = by_size(archive)
        declare_merge(archive, smaller, larger)

        for key in (smaller, larger):
            check.equal(
                len(composed_children(archive, key)),
                len(list(staged_children(archive, key))),
            )


########################################################################
########################################################################
#
class TestVerify:
    """Tests for reading an uploaded trip back and checking it."""

    ####################################################################
    #
    def test_a_trip_uploaded_whole_agrees_with_its_plan(
        self,
        staged: Archive,
        staged_key: str,
        api_client: TripsyClient,
        uploaded: TripImport,
    ) -> None:
        """
        GIVEN: a trip uploaded in full
        WHEN:  it is read back and compared
        THEN:  nothing is missing, nothing differs
        """
        result = verify_trip(api_client, staged, staged_key)

        check.is_true(result.agrees)
        check.equal(result.missing, [])
        check.equal(result.differing, [])
        check.equal(result.matched, result.planned)

    ####################################################################
    #
    def test_a_trip_never_uploaded_reads_as_wholly_missing(
        self, staged: Archive, staged_key: str, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a staged trip nothing has uploaded
        WHEN:  it is read back and compared
        THEN:  every planned object is reported missing

        Rather than an error: 'none of it is there' is an answer, and it
        is the answer after a run that failed before it started.
        """
        plan = plan_trip(staged, staged_key)

        result = verify_trip(api_client, staged, staged_key)

        check.is_none(result.trip_id)
        check.equal(len(result.missing), plan.total)
        check.is_false(result.agrees)

    ####################################################################
    #
    def test_an_object_deleted_in_the_app_reads_as_missing(
        self,
        staged: Archive,
        staged_key: str,
        api_client: TripsyClient,
        uploaded: TripImport,
    ) -> None:
        """
        GIVEN: an uploaded trip one of whose objects was then removed
        WHEN:  it is read back and compared
        THEN:  that object, and only that object, is reported missing

        Which is the case worth catching: the upload said it created the
        object, so nothing local knows it has gone.
        """
        assert uploaded.trip_id is not None
        gone = next(
            iter(
                child_ids_by_identifier(
                    api_client, uploaded.trip_id, "transportations"
                ).items()
            )
        )
        api_client.delete_child(uploaded.trip_id, "transportations", gone[1])

        checked = verify_trip(api_client, staged, staged_key)

        check.equal(checked.missing, [gone[0]])
        check.is_false(checked.agrees)

    ####################################################################
    #
    def test_an_object_added_in_the_app_is_reported_not_missed(
        self,
        staged: Archive,
        staged_key: str,
        api_client: TripsyClient,
        uploaded: TripImport,
    ) -> None:
        """
        GIVEN: an uploaded trip that gained an object in the app
        WHEN:  it is read back and compared
        THEN:  the object is reported as extra, and the trip still agrees

        Editing in the app is the point of the import, so an object the
        plan does not know about is news rather than a fault.
        """
        assert uploaded.trip_id is not None
        api_client.create_child(
            uploaded.trip_id,
            "activities",
            {"name": "added in the app", "internal_identifier": "app-1"},
        )

        checked = verify_trip(api_client, staged, staged_key)

        check.equal(checked.extra, ["app-1"])
        check.equal(
            checked.matched,
            checked.planned,
            "every planned object is still accounted for",
        )
        check.is_true(checked.agrees, "an addition is not a disagreement")

    ####################################################################
    #
    def test_a_trip_whose_identifier_changed_is_found_by_its_id(
        self,
        staged: Archive,
        staged_key: str,
        api_client: TripsyClient,
        uploaded: TripImport,
    ) -> None:
        """
        GIVEN: an uploaded trip that Tripsy now returns with a different
               internal_identifier, as a trip saved again in the app does
        WHEN:  it is read back and compared
        THEN:  it is found through the id its upload recorded, and agrees

        Found by identifier it would read as wholly missing, which is
        what happened to three real trips.
        """
        assert uploaded.trip_id is not None
        api_client.update_trip(
            uploaded.trip_id, {"internal_identifier": "ReassignedInTheApp01"}
        )

        checked = verify_trip(api_client, staged, staged_key)

        check.equal(checked.trip_id, uploaded.trip_id)
        check.equal(checked.missing, [])
        check.is_true(checked.agrees)

    ####################################################################
    #
    def test_a_recorded_trip_deleted_in_the_app_reads_as_missing(
        self,
        staged: Archive,
        staged_key: str,
        api_client: TripsyClient,
        uploaded: TripImport,
    ) -> None:
        """
        GIVEN: an uploaded trip, recorded by id, then deleted in the app
        WHEN:  it is read back and compared
        THEN:  every planned object is reported missing, not an error
        """
        assert uploaded.trip_id is not None
        plan = plan_trip(staged, staged_key)
        api_client.delete_trip(uploaded.trip_id)

        checked = verify_trip(api_client, staged, staged_key)

        check.is_none(checked.trip_id)
        check.equal(len(checked.missing), plan.total)
        check.is_false(checked.agrees)


########################################################################
########################################################################
#
class TestUnplaced:
    """Tests for finding what Tripsy holds an address for and no position."""

    ####################################################################
    #
    def test_both_ends_of_a_leg_are_looked_at(
        self, api_client: TripsyClient, uploaded: TripImport
    ) -> None:
        """
        GIVEN: an uploaded trip whose legs carry addresses and no position
        WHEN:  the trip is asked what is unplaced
        THEN:  a leg contributes one entry per end

        A leg is two places, and each is looked up separately: a ferry
        crossing has a port at either side.
        """
        assert uploaded.trip_id is not None

        found = unplaced_in(api_client, uploaded.trip_id)

        legs = [row for row in found if row.collection == "transportations"]
        check.is_true(legs, "the fixture has addressed legs")
        check.equal(
            {row.prefix for row in legs},
            {"departure_", "arrival_"},
            "both ends are offered",
        )

    ####################################################################
    #
    def test_placing_an_endpoint_moves_it_from_wanted_to_reference(
        self, api_client: TripsyClient, uploaded: TripImport
    ) -> None:
        """
        GIVEN: an uploaded trip, one of whose endpoints is given a position
        WHEN:  the trip is asked what it wants and what it already places
        THEN:  that endpoint has left the first and joined the second

        Which is what makes this a clean-up run: it asks Tripsy what is
        still missing rather than assuming anything, and what it has
        already placed is what a new answer gets measured against.
        """
        assert uploaded.trip_id is not None
        before = unplaced_in(api_client, uploaded.trip_id)
        placed_before = positions_in(api_client, uploaded.trip_id)
        target = next(
            row for row in before if row.collection == "transportations"
        )

        api_client.update_child(
            uploaded.trip_id,
            target.collection,
            target.child_id,
            {
                f"{target.prefix}latitude": 1.5,
                f"{target.prefix}longitude": 2.5,
            },
        )

        after = unplaced_in(api_client, uploaded.trip_id)
        gone = {(r.child_id, r.prefix) for r in before} - {
            (r.child_id, r.prefix) for r in after
        }
        check.equal(gone, {(target.child_id, target.prefix)})
        placed_after = positions_in(api_client, uploaded.trip_id)
        check.is_in((1.5, 2.5), placed_after)
        check.equal(
            len(placed_after),
            len(placed_before) + 1,
            "the airports the export placed are still counted too",
        )
