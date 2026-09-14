#!/usr/bin/env python
#
"""Test the corrections laid over what the parser inferred."""

# system imports
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check
from faker import Faker

# Project imports
from tests.ics_builder import build_calendar, to_ics
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


####################################################################
#
def staged(
    tmp_path: Path, faker: Faker, **kwargs: Any
) -> tuple[Archive, StagedTrip, ParsedCalendar]:
    """An archive holding one staged synthetic trip."""
    archive = Archive(tmp_path)
    parsed = parse(to_ics(build_calendar(faker, **kwargs)))
    return archive, stage(archive, parsed), parsed


########################################################################
########################################################################
#
class TestRetyped:
    """Tests for rebuilding an object as another kind."""

    ####################################################################
    #
    def test_whole_fields_split_across_departure_and_arrival(self) -> None:
        """
        GIVEN: an activity with a start, an end and a place
        WHEN:  it is retyped as a transportation
        THEN:  its times bracket the leg and its place is the arrival

        The place is the destination because that is what the source
        carries: a TripIt calendar's LOCATION and GEO name where the leg
        ends, measured across the whole reference corpus.  A journey
        named for both ends still only records one of them.
        """
        activity = Activity(
            internal_identifier="txim-x",
            name="JR Tokyo to Kyoto",
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
        self, target: type[CanonicalModel]
    ) -> None:
        """
        GIVEN: an activity
        WHEN:  it is retyped to any kind
        THEN:  its identifier and name come with it

        The identifier is what keys the file, so a retype that lost it
        would strand the object rather than move it.
        """
        activity = Activity(internal_identifier="txim-x", name="a name")

        moved = retyped(activity, target).model_dump()

        check.equal(moved["internal_identifier"], "txim-x")
        check.equal(moved["name"], "a name")

    ####################################################################
    #
    def test_round_trip_restores_the_whole_fields(self) -> None:
        """
        GIVEN: an activity retyped to a transportation
        WHEN:  it is retyped back
        THEN:  the original time and place return
        """
        activity = Activity(
            internal_identifier="txim-x",
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
        self, tmp_path: Path
    ) -> None:
        """
        GIVEN: corrections against a trip uuid
        WHEN:  they are saved
        THEN:  they sit outside trips/, so a differently-namespaced
               staging of the same trip still finds them
        """
        archive = Archive(tmp_path)
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
    def test_round_trip(self, tmp_path: Path) -> None:
        """
        GIVEN: a set of corrections
        WHEN:  it is saved and read back
        THEN:  the retype, the fields and the note all survive
        """
        archive = Archive(tmp_path)
        overrides = OverrideSet(trip_uuid="t1")
        overrides.retype("e1", "transportations")
        overrides.correct("e1", transportation_type="train")
        overrides.entries["e1"].note = "JR line"

        save_overrides(archive, overrides)
        loaded = load_overrides(archive, "t1")

        entry = loaded.entries["e1"]
        check.equal(entry.collection, "transportations")
        check.equal(entry.fields, {"transportation_type": "train"})
        check.equal(entry.note, "JR line")

    ####################################################################
    #
    def test_missing_file_is_an_empty_set(self, tmp_path: Path) -> None:
        """
        GIVEN: a trip with no corrections
        WHEN:  its corrections are loaded
        THEN:  an empty set comes back rather than an error
        """
        loaded = load_overrides(Archive(tmp_path), "never-corrected")

        check.equal(loaded.entries, {})
        check.equal(loaded.trip_uuid, "never-corrected")

    ####################################################################
    #
    def test_unknown_collection_is_refused(self, tmp_path: Path) -> None:
        """
        GIVEN: a retype naming a collection that does not exist
        WHEN:  it is recorded
        THEN:  it is refused, rather than failing later at apply time
        """
        overrides = OverrideSet(trip_uuid="t1")

        with pytest.raises(ValueError):
            overrides.retype("e1", "restaurants")


########################################################################
########################################################################
#
class TestAdditions:
    """Tests for objects a person added that no source record held."""

    ####################################################################
    #
    def test_round_trip(self, tmp_path: Path) -> None:
        """
        GIVEN: an addition recorded against a trip
        WHEN:  it is saved and read back
        THEN:  its collection, fields and note all survive
        """
        archive = Archive(tmp_path)
        overrides = OverrideSet(trip_uuid="t1")
        overrides.add(
            "a1",
            "transportations",
            name="LAX to the rental counter",
            transportation_type="transfer",
        )
        overrides.additions["a1"].note = "no source record"

        save_overrides(archive, overrides)
        addition = load_overrides(archive, "t1").additions["a1"]

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
    def test_a_set_with_no_additions_stores_none(self, tmp_path: Path) -> None:
        """
        GIVEN: corrections carrying no additions
        WHEN:  they are saved
        THEN:  no `additions` key is written

        Every trip has corrections; almost none have additions.
        """
        archive = Archive(tmp_path)
        overrides = OverrideSet(trip_uuid="t1")
        overrides.correct("e1", name="corrected")
        path = save_overrides(archive, overrides)

        assert "additions" not in json.loads(path.read_text())

    ####################################################################
    #
    def test_adding_twice_under_one_uuid_replaces(self, tmp_path: Path) -> None:
        """
        GIVEN: an addition recorded twice under the same uuid
        WHEN:  the set is read back
        THEN:  one addition is held, carrying the second set of fields

        The uuid is what makes re-running the hand that added it a no-op
        rather than a second shuttle bus.
        """
        archive = Archive(tmp_path)
        overrides = OverrideSet(trip_uuid="t1")
        overrides.add("a1", "activities", name="first")
        overrides.add("a1", "activities", name="second")

        save_overrides(archive, overrides)
        loaded = load_overrides(archive, "t1")

        check.equal(len(loaded.additions), 1)
        check.equal(loaded.additions["a1"].fields, {"name": "second"})

    ####################################################################
    #
    def test_unknown_collection_is_refused(self, tmp_path: Path) -> None:
        """
        GIVEN: an addition naming a collection that does not exist
        WHEN:  it is recorded
        THEN:  it is refused, rather than failing later at upload time
        """
        overrides = OverrideSet(trip_uuid="t1")

        with pytest.raises(ValueError):
            overrides.add("a1", "restaurants", name="dinner")

    ####################################################################
    #
    def test_a_field_that_cannot_be_stored_is_refused(self) -> None:
        """
        GIVEN: an addition whose instant is a datetime rather than text
        WHEN:  it is recorded
        THEN:  it is refused, naming the field and what to give instead

        Additions are stored as JSON, so a datetime fails at save time --
        long after the line that put it there.
        """
        from datetime import UTC, datetime

        overrides = OverrideSet(trip_uuid="t1")

        with pytest.raises(ValueError, match="departure_at"):
            overrides.add(
                "a1",
                "transportations",
                departure_at=datetime(2024, 5, 3, tzinfo=UTC),
            )

    ####################################################################
    #
    def test_applying_corrections_ignores_additions(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a trip whose corrections hold an addition and nothing else
        WHEN:  the corrections are applied to the archive
        THEN:  nothing is touched and no uuid is reported unknown

        An addition is not in the trip's index and never will be: it is
        applied on the way out, not written into the staged trip.
        """
        archive, trip, _ = staged(tmp_path, faker)
        overrides = OverrideSet(trip_uuid="t1")
        overrides.add("a1", "activities", name="added")

        applied = apply_overrides(archive, trip.trip_key, overrides)

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
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a staged activity
        WHEN:  it is retyped to a transportation
        THEN:  it appears in transportations and is gone from activities,
               under the same filename
        """
        archive, trip, parsed = staged(tmp_path, faker, items=1)
        uuid = uuid_from_uid(parsed.notes[0].uid)
        identifier = parsed.notes[0].identifier
        overrides = OverrideSet(trip_uuid="t1")
        overrides.retype(str(uuid), "transportations")

        applied = apply_overrides(archive, trip.trip_key, overrides)

        trip_dir = archive.trip_dir(trip.trip_key)
        check.equal(applied.retyped, 1)
        check.is_true(
            (trip_dir / "transportations" / f"{identifier}.json").exists()
        )
        check.is_false(
            (trip_dir / "activities" / f"{identifier}.json").exists(),
            "the original must be gone, not left beside the retyped copy",
        )

    ####################################################################
    #
    def test_retype_carries_the_times_over(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a staged activity with a start and an end
        WHEN:  it is retyped to a transportation
        THEN:  the stored object has a departure and an arrival
        """
        archive, trip, parsed = staged(tmp_path, faker, items=1)
        activity = parsed.activities[0]
        uuid = str(uuid_from_uid(parsed.notes[0].uid))
        overrides = OverrideSet(trip_uuid="t1")
        overrides.retype(uuid, "transportations")

        apply_overrides(archive, trip.trip_key, overrides)

        path = (
            archive.trip_dir(trip.trip_key)
            / "transportations"
            / f"{activity.internal_identifier}.json"
        )
        stored = archive.read(Transportation, path)
        assert stored is not None
        check.equal(stored.departure_at, activity.starts_at)
        check.equal(stored.arrival_at, activity.ends_at)

    ####################################################################
    #
    def test_field_corrections_are_applied(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a staged activity
        WHEN:  a field correction is applied
        THEN:  the stored object carries the corrected value
        """
        archive, trip, parsed = staged(tmp_path, faker, items=1)
        activity = parsed.activities[0]
        uuid = str(uuid_from_uid(parsed.notes[0].uid))
        overrides = OverrideSet(trip_uuid="t1")
        overrides.correct(uuid, activity_type="museum", name="Corrected")

        applied = apply_overrides(archive, trip.trip_key, overrides)

        path = (
            archive.trip_dir(trip.trip_key)
            / "activities"
            / f"{activity.internal_identifier}.json"
        )
        stored = archive.read(Activity, path)
        assert stored is not None
        check.equal(applied.corrected, 1)
        check.equal(stored.activity_type, "museum")
        check.equal(stored.name, "Corrected")

    ####################################################################
    #
    def test_unknown_uuid_is_reported_not_raised(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a correction against a uuid this trip does not contain
        WHEN:  corrections are applied
        THEN:  it is reported, and the rest of the run continues
        """
        archive, trip, parsed = staged(tmp_path, faker, items=1)
        overrides = OverrideSet(trip_uuid="t1")
        overrides.correct("not-in-this-trip", name="x")

        applied = apply_overrides(archive, trip.trip_key, overrides)

        check.equal(applied.unknown, ["not-in-this-trip"])
        check.equal(applied.total, 0)

    ####################################################################
    #
    def test_staging_does_not_apply_corrections(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: corrections saved for a trip
        WHEN:  the trip is staged again
        THEN:  the staged object is what the parser produced

        Staging has to stay lossless, or 'what the parser inferred' stops
        being recoverable and the overlay has nothing to sit on top of.
        """
        archive = Archive(tmp_path)
        text = to_ics(build_calendar(faker, items=1))
        parsed = parse(text)
        trip = stage(archive, parsed)
        uuid = str(uuid_from_uid(parsed.notes[0].uid))

        overrides = OverrideSet(trip_uuid="t1")
        overrides.correct(uuid, name="Corrected")
        save_overrides(archive, overrides)
        apply_overrides(archive, trip.trip_key, overrides)
        stage(archive, parse(text))

        path = (
            archive.trip_dir(trip.trip_key)
            / "activities"
            / f"{parsed.activities[0].internal_identifier}.json"
        )
        stored = archive.read(Activity, path)
        assert stored is not None
        check.equal(stored.name, parsed.activities[0].name)


########################################################################
########################################################################
#
class TestIndex:
    """Tests for the uuid map staging writes."""

    ####################################################################
    #
    def test_every_object_is_indexed(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a trip staged from a calendar of mixed kinds
        WHEN:  its report is read
        THEN:  every child object is reachable by its source uuid
        """
        archive, trip, parsed = staged(
            tmp_path, faker, items=6, lodging=2, flights=2
        )

        document = json.loads(trip.report_path.read_text())

        index = document["index"]
        check.equal(len(index), trip.total)
        for note in parsed.notes:
            uuid = str(uuid_from_uid(note.uid))
            check.is_in(uuid, index)
            check.equal(index[uuid]["identifier"], note.identifier)
