#!/usr/bin/env python
#
"""
Test the corrections laid over what the parser inferred.

Most of these need a staged trip with one object in it and a correction
against that object's source uuid, which is three lines of derivation
before a test can say what it is actually correcting.  `Staged` carries
those, so a test names the fixture and asserts.
"""

# system imports
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check

# Project imports
from tripsy_exim.models import (
    Activity,
    CanonicalModel,
    Hosting,
    Transportation,
)
from tripsy_exim.sources import ParsedCalendar, parse, uuid_from_uid
from tripsy_exim.store import Archive
from tripsy_exim.sync import (
    OverrideSet,
    StagedTrip,
    apply_overrides,
    load_overrides,
    retyped,
    save_overrides,
    stage,
)

# The uuid the corrections fixtures are keyed by.  Only the test about
# where a file lands cares what it actually says.
#
TRIP_UUID = "t1"


########################################################################
########################################################################
#
@dataclass
class Staged:
    """
    One staged trip, and the handles a correction against it needs.

    A correction is keyed by the source record's uuid, which is derived
    from the reader's note rather than held on the object, and a test
    checking the result has to rebuild the path the object was written
    to.  Both live here so neither is spelled out again in a test.
    """

    archive: Archive
    trip: StagedTrip
    parsed: ParsedCalendar
    text: str

    ####################################################################
    #
    @property
    def key(self) -> str:
        """The trip key the archive filed this under."""
        return self.trip.trip_key

    ####################################################################
    #
    @property
    def activity(self) -> Activity:
        """The trip's first activity, which most corrections are about."""
        return self.parsed.activities[0]

    ####################################################################
    #
    @property
    def uuid(self) -> str:
        """The source uuid of the first object, as a correction keys it."""
        return str(uuid_from_uid(self.parsed.notes[0].uid))

    ####################################################################
    #
    @property
    def identifier(self) -> str:
        """The minted identifier of the first object, as a file is named."""
        return str(self.parsed.notes[0].identifier)

    ####################################################################
    #
    def path_in(self, collection: str) -> Path:
        """Where the first object sits if it belongs to this collection."""
        return (
            self.archive.trip_dir(self.key)
            / collection
            / f"{self.identifier}.json"
        )

    ####################################################################
    #
    def stored[M: CanonicalModel](self, model: type[M], collection: str) -> M:
        """Read the first object back from the archive, as this kind."""
        found = self.archive.read(model, self.path_in(collection))
        assert found is not None, f"nothing stored in {collection}"
        return found

    ####################################################################
    #
    def stage_again(self) -> None:
        """Parse and stage the same calendar a second time."""
        stage(self.archive, parse(self.text))


####################################################################
#
@pytest.fixture
def staged(
    archive: Archive, ics_calendar: Callable[..., str]
) -> Callable[..., Staged]:
    """
    Stage one synthetic trip, taking `build_calendar`'s keywords.

    A test asks for the shape of calendar it needs -- how many items, how
    many stays -- and gets back the archive it landed in along with what
    the parser made of it.
    """

    def build(**kwargs: Any) -> Staged:
        text = ics_calendar(**kwargs)
        parsed = parse(text)
        return Staged(archive, stage(archive, parsed), parsed, text)

    return build


####################################################################
#
@pytest.fixture
def one_activity(staged: Callable[..., Staged]) -> Staged:
    """A staged trip of exactly one activity, which is what a correction
    is usually against."""
    return staged(items=1)


####################################################################
#
@pytest.fixture
def overrides() -> OverrideSet:
    """An empty correction set, keyed to the trip the fixtures stage."""
    return OverrideSet(trip_uuid=TRIP_UUID)


########################################################################
########################################################################
#
class TestRetyped:
    """Tests for rebuilding an object as another kind."""

    ####################################################################
    #
    def test_whole_fields_split_across_departure_and_arrival(
        self, activity_factory: Callable[..., Activity]
    ) -> None:
        """
        GIVEN: an activity with a start, an end and a place
        WHEN:  it is retyped as a transportation
        THEN:  its times bracket the leg and its place is the arrival

        The place is the destination because that is what the source
        carries: a TripIt calendar's LOCATION and GEO name where the leg
        ends, measured across the whole reference corpus.  A journey
        named for both ends still only records one of them.
        """
        activity = activity_factory(
            starts_at=datetime(2027, 1, 1, 9, tzinfo=UTC),
            ends_at=datetime(2027, 1, 1, 11, tzinfo=UTC),
            timezone="Asia/Tokyo",
            address="Kyoto Station",
        )

        moved = retyped(activity, Transportation)

        check.equal(moved.departure_at, activity.starts_at)
        check.equal(moved.arrival_at, activity.ends_at)
        check.equal(moved.arrival_timezone, "Asia/Tokyo")
        check.equal(moved.arrival_address, "Kyoto Station")
        check.is_none(moved.departure_address, "the origin is not recorded")

    ####################################################################
    #
    @pytest.mark.parametrize(
        "target", [Hosting, Transportation, Activity], ids=lambda t: t.__name__
    )
    def test_identity_and_name_always_survive(
        self,
        target: type[CanonicalModel],
        activity: Activity,
    ) -> None:
        """
        GIVEN: an activity
        WHEN:  it is retyped to any kind
        THEN:  its identifier and name come with it

        The identifier is what keys the file, so a retype that lost it
        would strand the object rather than move it.
        """
        moved = retyped(activity, target).model_dump()

        check.equal(moved["internal_identifier"], activity.internal_identifier)
        check.equal(moved["name"], activity.name)

    ####################################################################
    #
    def test_round_trip_restores_the_whole_fields(
        self, activity_factory: Callable[..., Activity]
    ) -> None:
        """
        GIVEN: an activity retyped to a transportation
        WHEN:  it is retyped back
        THEN:  the original time and place return
        """
        activity = activity_factory(
            starts_at=datetime(2027, 1, 1, 9, tzinfo=UTC),
            ends_at=datetime(2027, 1, 1, 11, tzinfo=UTC),
            address="Tokyo Station",
        )

        back = retyped(retyped(activity, Transportation), Activity)

        check.equal(back.starts_at, activity.starts_at)
        check.equal(back.ends_at, activity.ends_at)
        check.equal(back.address, activity.address)


########################################################################
########################################################################
#
class TestOverrideStorage:
    """Tests for where corrections live and how they round trip."""

    ####################################################################
    #
    def test_overrides_live_outside_the_trip_directories(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: corrections against a trip uuid
        WHEN:  they are saved
        THEN:  they sit outside trips/, so a differently-namespaced
               staging of the same trip still finds them

        Keyed by its own uuid rather than by the fixture's, since what
        is under test is the filename.
        """
        overrides = OverrideSet(trip_uuid="trip-uuid-1")
        overrides.retype("event-1", "transportations")

        path = save_overrides(archive, overrides)

        check.is_false(
            (archive.root / "trips") in path.parents,
            "corrections must not be keyed by a namespaced trip key",
        )
        check.equal(path.name, "trip-uuid-1.json")

    ####################################################################
    #
    def test_round_trip(self, archive: Archive, overrides: OverrideSet) -> None:
        """
        GIVEN: a set of corrections
        WHEN:  it is saved and read back
        THEN:  the retype, the fields and the note all survive
        """
        overrides.retype("e1", "transportations")
        overrides.correct("e1", transportation_type="train")
        overrides.entries["e1"].note = "JR line"

        save_overrides(archive, overrides)
        loaded = load_overrides(archive, TRIP_UUID)

        entry = loaded.entries["e1"]
        check.equal(entry.collection, "transportations")
        check.equal(entry.fields, {"transportation_type": "train"})
        check.equal(entry.note, "JR line")

    ####################################################################
    #
    def test_missing_file_is_an_empty_set(self, archive: Archive) -> None:
        """
        GIVEN: a trip with no corrections
        WHEN:  its corrections are loaded
        THEN:  an empty set comes back rather than an error
        """
        loaded = load_overrides(archive, "never-corrected")

        check.equal(loaded.entries, {})
        check.equal(loaded.trip_uuid, "never-corrected")

    ####################################################################
    #
    def test_unknown_collection_is_refused(
        self, overrides: OverrideSet
    ) -> None:
        """
        GIVEN: a retype naming a collection that does not exist
        WHEN:  it is recorded
        THEN:  it is refused, rather than failing later at apply time
        """
        with pytest.raises(ValueError):
            overrides.retype("e1", "restaurants")


########################################################################
########################################################################
#
class TestAdditions:
    """Tests for objects a person added that no source record held."""

    ####################################################################
    #
    def test_round_trip(self, archive: Archive, overrides: OverrideSet) -> None:
        """
        GIVEN: an addition recorded against a trip
        WHEN:  it is saved and read back
        THEN:  its collection, fields and note all survive
        """
        overrides.add(
            "a1",
            "transportations",
            name="LAX to the rental counter",
            transportation_type="transfer",
        )
        overrides.additions["a1"].note = "no source record"

        save_overrides(archive, overrides)
        addition = load_overrides(archive, TRIP_UUID).additions["a1"]

        check.equal(addition.collection, "transportations")
        check.equal(
            addition.fields,
            {
                "name": "LAX to the rental counter",
                "transportation_type": "transfer",
            },
        )
        check.equal(addition.note, "no source record")

    ####################################################################
    #
    def test_a_set_with_no_additions_stores_none(
        self, archive: Archive, overrides: OverrideSet
    ) -> None:
        """
        GIVEN: corrections carrying no additions
        WHEN:  they are saved
        THEN:  no `additions` key is written

        Every trip has corrections; almost none have additions.
        """
        overrides.correct("e1", name="corrected")

        path = save_overrides(archive, overrides)

        assert "additions" not in json.loads(path.read_text())

    ####################################################################
    #
    def test_adding_twice_under_one_uuid_replaces(
        self, archive: Archive, overrides: OverrideSet
    ) -> None:
        """
        GIVEN: an addition recorded twice under the same uuid
        WHEN:  the set is read back
        THEN:  one addition is held, carrying the second set of fields

        The uuid is what makes re-running the hand that added it a no-op
        rather than a second shuttle bus.
        """
        overrides.add("a1", "activities", name="first")
        overrides.add("a1", "activities", name="second")

        save_overrides(archive, overrides)
        loaded = load_overrides(archive, TRIP_UUID)

        check.equal(len(loaded.additions), 1)
        check.equal(loaded.additions["a1"].fields, {"name": "second"})

    ####################################################################
    #
    @pytest.mark.parametrize(
        "collection,fields,names",
        [
            pytest.param(
                "restaurants",
                {"name": "dinner"},
                "restaurants",
                id="a-collection-tripsy-does-not-have",
            ),
            pytest.param(
                "transportations",
                {"departure_at": datetime(2024, 5, 3, tzinfo=UTC)},
                "departure_at",
                id="a-value-that-cannot-be-stored",
            ),
        ],
    )
    def test_what_cannot_be_stored_is_refused_when_recorded(
        self,
        overrides: OverrideSet,
        collection: str,
        fields: dict[str, Any],
        names: str,
    ) -> None:
        """
        GIVEN: an addition naming an impossible collection or value
        WHEN:  it is recorded
        THEN:  it is refused, naming what was wrong

        Additions are held as JSON, so a datetime failed at save time --
        long after the line that put it there.  A bad collection failed
        later still, at upload.
        """
        with pytest.raises(ValueError, match=names):
            overrides.add("a1", collection, **fields)

    ####################################################################
    #
    def test_applying_corrections_ignores_additions(
        self, one_activity: Staged, overrides: OverrideSet
    ) -> None:
        """
        GIVEN: a trip whose corrections hold an addition and nothing else
        WHEN:  the corrections are applied to the archive
        THEN:  nothing is touched and no uuid is reported unknown

        An addition is not in the trip's index and never will be: it is
        applied on the way out, not written into the staged trip.
        """
        overrides.add("a1", "activities", name="added")

        applied = apply_overrides(
            one_activity.archive, one_activity.key, overrides
        )

        check.equal(applied.total, 0)
        check.equal(applied.unknown, [])


########################################################################
########################################################################
#
class TestApplyOverrides:
    """Tests for laying corrections over a staged trip."""

    ####################################################################
    #
    def test_retype_moves_the_file_between_collections(
        self, one_activity: Staged, overrides: OverrideSet
    ) -> None:
        """
        GIVEN: a staged activity
        WHEN:  it is retyped to a transportation
        THEN:  it appears in transportations and is gone from activities,
               under the same filename
        """
        overrides.retype(one_activity.uuid, "transportations")

        applied = apply_overrides(
            one_activity.archive, one_activity.key, overrides
        )

        check.equal(applied.retyped, 1)
        check.is_true(one_activity.path_in("transportations").exists())
        check.is_false(
            one_activity.path_in("activities").exists(),
            "the original must be gone, not left beside the retyped copy",
        )

    ####################################################################
    #
    def test_retype_carries_the_times_over(
        self, one_activity: Staged, overrides: OverrideSet
    ) -> None:
        """
        GIVEN: a staged activity with a start and an end
        WHEN:  it is retyped to a transportation
        THEN:  the stored object has a departure and an arrival
        """
        overrides.retype(one_activity.uuid, "transportations")

        apply_overrides(one_activity.archive, one_activity.key, overrides)

        stored = one_activity.stored(Transportation, "transportations")
        check.equal(stored.departure_at, one_activity.activity.starts_at)
        check.equal(stored.arrival_at, one_activity.activity.ends_at)

    ####################################################################
    #
    def test_field_corrections_are_applied(
        self, one_activity: Staged, overrides: OverrideSet
    ) -> None:
        """
        GIVEN: a staged activity
        WHEN:  a field correction is applied
        THEN:  the stored object carries the corrected value
        """
        overrides.correct(
            one_activity.uuid, activity_type="museum", name="Corrected"
        )

        applied = apply_overrides(
            one_activity.archive, one_activity.key, overrides
        )

        stored = one_activity.stored(Activity, "activities")
        check.equal(applied.corrected, 1)
        check.equal(stored.activity_type, "museum")
        check.equal(stored.name, "Corrected")

    ####################################################################
    #
    def test_unknown_uuid_is_reported_not_raised(
        self, one_activity: Staged, overrides: OverrideSet
    ) -> None:
        """
        GIVEN: a correction against a uuid this trip does not contain
        WHEN:  corrections are applied
        THEN:  it is reported, and the rest of the run continues
        """
        overrides.correct("not-in-this-trip", name="x")

        applied = apply_overrides(
            one_activity.archive, one_activity.key, overrides
        )

        check.equal(applied.unknown, ["not-in-this-trip"])
        check.equal(applied.total, 0)

    ####################################################################
    #
    def test_staging_does_not_apply_corrections(
        self, one_activity: Staged, overrides: OverrideSet
    ) -> None:
        """
        GIVEN: corrections saved for a trip
        WHEN:  the trip is staged again
        THEN:  the staged object is what the parser produced

        Staging has to stay lossless, or 'what the parser inferred' stops
        being recoverable and the overlay has nothing to sit on top of.
        """
        overrides.correct(one_activity.uuid, name="Corrected")
        save_overrides(one_activity.archive, overrides)
        apply_overrides(one_activity.archive, one_activity.key, overrides)

        one_activity.stage_again()

        stored = one_activity.stored(Activity, "activities")
        check.equal(stored.name, one_activity.activity.name)


########################################################################
########################################################################
#
class TestIndex:
    """Tests for the uuid map staging writes."""

    ####################################################################
    #
    def test_every_object_is_indexed(
        self, staged: Callable[..., Staged]
    ) -> None:
        """
        GIVEN: a trip staged from a calendar of mixed kinds
        WHEN:  its report is read
        THEN:  every child object is reachable by its source uuid
        """
        trip = staged(items=6, lodging=2, flights=2)

        document = json.loads(trip.trip.report_path.read_text())

        index = document["index"]
        check.equal(len(index), trip.trip.total)
        for note in trip.parsed.notes:
            uuid = str(uuid_from_uid(note.uid))
            check.is_in(uuid, index)
            check.equal(index[uuid]["identifier"], note.identifier)
