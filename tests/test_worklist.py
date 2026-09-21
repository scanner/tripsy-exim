#!/usr/bin/env python
#
"""
Test the work-list round trip.

The file is edited by hand between being written and being read back, so
what matters here is what survives that: a row someone filled in, a row
they left alone, a value they typed as text that has to become a number,
and a second run over the same file doing nothing at all.

`edited` is how a test says what a person did to the file -- it exports,
hands the rows to a callback, and applies the result.
"""

# system imports
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check

# Project imports
from tripsy_exim.models import Transportation
from tripsy_exim.store import Archive
from tripsy_exim.sync.backfill import (
    GUESSED_TIMEZONE,
    SKIPPED,
    UNCLASSIFIED,
    Gap,
    gaps,
)
from tripsy_exim.sync.importer import (
    Child,
    composed_children,
    staged_children,
)
from tripsy_exim.sync.worklist import (
    VERSION,
    Outcome,
    WorkListError,
    apply_rows,
    export_rows,
    field_names,
    read_worklist,
    write_worklist,
)

# What a filled-in row says, when a test does not care which airport it
# is answering.  Suffixed because a bare place name elsewhere in the
# suite is a position: this one is the text of an address.
#
NARITA_ADDRESS = "Narita International Airport"


####################################################################
#
@pytest.fixture
def rows_for(archive: Archive) -> Callable[[str], list[dict[str, Any]]]:
    """The work-list rows one staged trip would export."""

    def build(trip_key: str) -> list[dict[str, Any]]:
        return export_rows(archive, [trip_key])

    return build


####################################################################
#
@pytest.fixture
def edited(
    archive: Archive, rows_for: Callable[[str], list[dict[str, Any]]]
) -> Callable[..., Outcome]:
    """
    Export a trip, let a test edit the rows, then apply them.

    This is the whole round trip in one call, because every test here is
    about what an edit does rather than about the two halves separately.
    """

    def run(
        trip_key: str,
        edit: Callable[[list[dict[str, Any]]], None] | None = None,
        write: bool = True,
    ) -> Outcome:
        rows = rows_for(trip_key)
        if edit is not None:
            edit(rows)
        return apply_rows(archive, rows, write=write)

    return run


####################################################################
#
def fill_departures(rows: list[dict[str, Any]]) -> None:
    """Answer every departure endpoint, leaving the arrivals alone."""
    for row in rows:
        if row.get("object", "").endswith("(departure)"):
            row["fields"]["departure_address"] = NARITA_ADDRESS


########################################################################
########################################################################
#
class TestExport:
    """Tests for writing the work-list out."""

    ####################################################################
    #
    def test_a_row_carries_the_fields_it_would_set(
        self, unplaceable_trip: str, rows_for: Callable[..., list]
    ) -> None:
        """
        GIVEN: a trip with unplaceable endpoints
        WHEN:  the work-list is exported
        THEN:  each row names the model fields, prefixed by its endpoint

        Real field names rather than logical ones is what makes applying
        a row mechanical, and what makes the file say plainly what it
        will set.
        """
        rows = rows_for(unplaceable_trip)

        departure = next(r for r in rows if r["object"].endswith("(departure)"))

        check.equal(
            set(departure["fields"]),
            {
                "departure_address",
                "departure_latitude",
                "departure_longitude",
            },
        )

    ####################################################################
    #
    def test_unplaceable_rows_arrive_blank(
        self, unplaceable_trip: str, rows_for: Callable[..., list]
    ) -> None:
        """
        GIVEN: a trip whose endpoints carry neither address nor position
        WHEN:  the work-list is exported
        THEN:  every offered field is blank

        Blank is not an accident here -- it is what the object holds.
        Filling one in is the whole action.
        """
        rows = rows_for(unplaceable_trip)

        values = {v for row in rows for v in row["fields"].values()}

        check.equal(values, {""})

    ####################################################################
    #
    def test_a_row_names_the_uuid_and_not_only_the_identifier(
        self, unplaceable_trip: str, rows_for: Callable[..., list]
    ) -> None:
        """
        GIVEN: an exported work-list
        WHEN:  a row is read
        THEN:  it carries the uuid a correction is keyed by

        The identifier is there too, because that is what finds the
        object; the uuid is what the override is filed under.
        """
        for row in rows_for(unplaceable_trip):
            check.is_true(row["uuid"])
            check.is_true(row["identifier"])

    ####################################################################
    #
    def test_the_file_records_where_it_came_from(
        self, unplaceable_trip: str, archive: Archive, tmp_path: Path
    ) -> None:
        """
        GIVEN: a work-list written to disk
        WHEN:  the file is read as JSON
        THEN:  it carries its version and its archive

        A work-list applied to the wrong archive would match no uuids at
        all, which reads as "nothing to do" rather than as a mistake.
        """
        path = tmp_path / "work.json"

        write_worklist(path, archive, export_rows(archive, [unplaceable_trip]))

        document = json.loads(path.read_text())
        check.equal(document["version"], VERSION)
        check.equal(document["archive"], str(archive.root))
        check.is_true(document["generated_at"])


########################################################################
########################################################################
#
class TestApply:
    """Tests for reading an edited work-list back."""

    ####################################################################
    #
    def test_a_filled_row_becomes_a_correction(
        self,
        unplaceable_trip: str,
        edited: Callable[..., Outcome],
        archive: Archive,
    ) -> None:
        """
        GIVEN: a work-list with its departure rows filled in
        WHEN:  it is applied
        THEN:  those gaps are closed and the untouched ones remain

        The correction is laid over on the way out, so the gap closing
        is what proves it was written where the reader will find it.
        """
        outcome = edited(unplaceable_trip, fill_departures)

        check.equal(outcome.corrected, 2)
        check.equal(outcome.left_blank, 2, "the arrivals")
        left = gaps(archive, unplaceable_trip)
        check.equal(len(left), 2)
        check.equal({g.endpoint for g in left}, {"arrival"})

    ####################################################################
    #
    def test_applying_the_same_file_twice_changes_nothing(
        self,
        unplaceable_trip: str,
        rows_for: Callable[..., list],
        archive: Archive,
    ) -> None:
        """
        GIVEN: one work-list, applied once
        WHEN:  the very same rows are applied again
        THEN:  nothing is written and they read as already correct

        The same file, deliberately, rather than a fresh export: an
        export taken afterwards would no longer carry the answered rows
        at all, which proves something different.  Only what differs is
        written, so a re-run over a file somebody kept is a no-op.
        """
        rows = rows_for(unplaceable_trip)
        fill_departures(rows)
        apply_rows(archive, rows, write=True)

        again = apply_rows(archive, rows, write=True)

        check.equal(again.written, 0)
        check.equal(again.unchanged, 2)
        check.equal(again.left_blank, 2, "the arrivals, still blank")

    ####################################################################
    #
    def test_a_second_work_list_does_not_discard_the_first(
        self,
        unplaceable_trip: str,
        edited: Callable[..., Outcome],
        archive: Archive,
    ) -> None:
        """
        GIVEN: one work-list applied, answering the departures
        WHEN:  a second is applied answering the arrivals
        THEN:  both answers are kept

        A trip holds all of its corrections in one file, so applying the
        second has to load and merge rather than construct a fresh set.
        """
        edited(unplaceable_trip, fill_departures)

        def fill_arrivals(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                if row["object"].endswith("(arrival)"):
                    row["fields"]["arrival_address"] = "Somewhere else"

        edited(unplaceable_trip, fill_arrivals)

        check.equal(gaps(archive, unplaceable_trip), [])

    ####################################################################
    #
    @pytest.mark.parametrize(
        "value,why",
        [
            ("", "a blank left deliberately blank"),
            (None, "a row nothing was typed into"),
        ],
    )
    def test_a_blank_is_left_alone(
        self,
        value: Any,
        why: str,
        unplaceable_trip: str,
        edited: Callable[..., Outcome],
    ) -> None:
        """
        GIVEN: a work-list whose rows hold no value
        WHEN:  it is applied
        THEN:  nothing is written

        An empty string is an answer of "leave this", not an answer of
        "set this to nothing".
        """

        def blank(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                for name in row["fields"]:
                    row["fields"][name] = value

        outcome = edited(unplaceable_trip, blank)

        check.equal(outcome.written, 0, why)
        check.equal(outcome.left_blank, 4)

    ####################################################################
    #
    def test_a_note_key_is_never_written(
        self,
        unplaceable_trip: str,
        edited: Callable[..., Outcome],
        archive: Archive,
    ) -> None:
        """
        GIVEN: a row carrying a key ending in '_note'
        WHEN:  it is applied
        THEN:  the note is not stored with the correction

        A note is commentary somebody left themselves.  Storing it would
        put it on the object and send it to Tripsy.
        """

        def annotate(rows: list[dict[str, Any]]) -> None:
            fill_departures(rows)
            for row in rows:
                row["fields"]["departure_note"] = "why I chose this"

        edited(unplaceable_trip, annotate)

        stored = json.loads(
            next((archive.root / "overrides").glob("*.json")).read_text()
        )
        written = {
            name
            for entry in stored["entries"].values()
            for name in entry.get("fields", {})
        }
        check.is_not_in("departure_note", written)

    ####################################################################
    #
    def test_a_number_typed_as_text_is_stored_as_a_number(
        self,
        unplaceable_trip: str,
        edited: Callable[..., Outcome],
        archive: Archive,
    ) -> None:
        """
        GIVEN: a row whose latitude was typed as a string
        WHEN:  it is applied
        THEN:  a number is what gets stored

        Corrections are laid over with `model_copy`, which does not
        validate, so an unchecked string would reach Tripsy as a string.
        The row is validated against its model and the validated form is
        what is kept.
        """

        def typed_as_text(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                if row["object"].endswith("(departure)"):
                    row["fields"]["departure_latitude"] = "35.7720"

        edited(unplaceable_trip, typed_as_text)

        stored = json.loads(
            next((archive.root / "overrides").glob("*.json")).read_text()
        )
        values = [
            entry["fields"]["departure_latitude"]
            for entry in stored["entries"].values()
            if "departure_latitude" in entry.get("fields", {})
        ]
        check.equal(values, [35.772, 35.772])
        for value in values:
            check.is_instance(value, float)

    ####################################################################
    #
    def test_a_value_the_model_refuses_is_reported_not_stored(
        self, unplaceable_trip: str, edited: Callable[..., Outcome]
    ) -> None:
        """
        GIVEN: a row whose latitude is not a number at all
        WHEN:  it is applied
        THEN:  the row is refused by name and nothing is written

        Refusing here is the point: the alternative is discovering it
        at upload, against the live API, halfway through a trip.
        """

        def nonsense(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                if row["object"].endswith("(departure)"):
                    row["fields"]["departure_latitude"] = "not a latitude"

        outcome = edited(unplaceable_trip, nonsense)

        check.equal(outcome.written, 0)
        check.equal(len(outcome.refused), 2)
        check.is_in("NAR", outcome.refused[0][0])

    ####################################################################
    #
    def test_a_dry_run_writes_nothing(
        self,
        unplaceable_trip: str,
        edited: Callable[..., Outcome],
        archive: Archive,
    ) -> None:
        """
        GIVEN: a filled-in work-list
        WHEN:  it is applied without write
        THEN:  it reports what it would do and the gaps remain open
        """
        outcome = edited(unplaceable_trip, fill_departures, write=False)

        check.equal(outcome.corrected, 2)
        check.equal(len(gaps(archive, unplaceable_trip)), 4, "still open")

    ####################################################################
    #
    def test_a_row_for_an_unstaged_trip_is_refused(
        self, unplaceable_trip: str, archive: Archive
    ) -> None:
        """
        GIVEN: a row naming a trip key the archive does not hold
        WHEN:  it is applied
        THEN:  it is refused rather than silently doing nothing
        """
        outcome = apply_rows(
            archive,
            [{"trip_key": "nowhere", "uuid": "u", "object": "a thing"}],
            write=True,
        )

        check.equal(outcome.written, 0)
        check.equal(len(outcome.refused), 1)
        check.is_in("nowhere", outcome.refused[0][1])


########################################################################
########################################################################
#
class TestReadWorkList:
    """Tests for refusing a file that is not this archive's work-list."""

    ####################################################################
    #
    def test_a_file_from_another_archive_is_refused(
        self, unplaceable_trip: str, archive: Archive, tmp_path: Path
    ) -> None:
        """
        GIVEN: a work-list exported from a different archive
        WHEN:  it is read
        THEN:  it is refused, naming both archives

        Applied anyway, it would match no uuids and report that nothing
        needed doing, which is the wrong answer to the wrong question.
        """
        path = tmp_path / "work.json"
        write_worklist(path, archive, export_rows(archive, [unplaceable_trip]))

        elsewhere = Archive(tmp_path / "elsewhere")

        with pytest.raises(WorkListError) as raised:
            read_worklist(path, elsewhere)

        check.is_in(str(archive.root), str(raised.value))

    ####################################################################
    #
    @pytest.mark.parametrize(
        "document,expected",
        [
            ({"version": 99, "rows": []}, "version"),
            ({"version": VERSION}, "no rows"),
            ([], "not a work-list"),
        ],
        ids=["another version", "no rows", "not an object"],
    )
    def test_a_file_this_cannot_read_is_refused(
        self,
        document: Any,
        expected: str,
        archive: Archive,
        tmp_path: Path,
    ) -> None:
        """
        GIVEN: a file that is not a work-list this version can read
        WHEN:  it is read
        THEN:  it is refused with a reason
        """
        path = tmp_path / "work.json"
        path.write_text(json.dumps(document))

        with pytest.raises(WorkListError) as raised:
            read_worklist(path, archive)

        check.is_in(expected, str(raised.value))

    ####################################################################
    #
    def test_a_round_trip_through_disk_keeps_the_rows(
        self, unplaceable_trip: str, archive: Archive, tmp_path: Path
    ) -> None:
        """
        GIVEN: a work-list written to disk
        WHEN:  it is read back
        THEN:  the rows are what was written
        """
        path = tmp_path / "work.json"
        rows = export_rows(archive, [unplaceable_trip])

        write_worklist(path, archive, rows)

        check.equal(read_worklist(path, archive), rows)


########################################################################
########################################################################
#
class TestComposedView:
    """Tests that the work-list reads a trip as it will upload."""

    ####################################################################
    #
    def test_the_staged_files_are_never_touched(
        self,
        unplaceable_trip: str,
        edited: Callable[..., Outcome],
        archive: Archive,
    ) -> None:
        """
        GIVEN: a work-list applied against a staged trip
        WHEN:  the staged objects are read off disk
        THEN:  none of them carries the correction

        Staging owns the files under a trip and a re-stage rewrites
        them, so a correction has to live in the override file instead.
        """
        edited(unplaceable_trip, fill_departures)

        on_disk = _legs(staged_children(archive, unplaceable_trip))
        composed = _legs(composed_children(archive, unplaceable_trip))

        check.equal({o.departure_address for o in on_disk}, {None})
        check.is_in(NARITA_ADDRESS, {o.departure_address for o in composed})


####################################################################
#
def _legs(objects: Iterable[Child]) -> list[Transportation]:
    """Only the legs, which are the objects that keep two places."""
    return [obj for obj in objects if isinstance(obj, Transportation)]


########################################################################
########################################################################
#
class TestRetypeAndAdd:
    """Tests for the two remedies that are not field corrections."""

    ####################################################################
    #
    def test_a_changed_collection_becomes_a_retype(
        self, mixed_gaps_trip: str, edited: Callable[..., Outcome]
    ) -> None:
        """
        GIVEN: a work-list row for an object no rule matched
        WHEN:  its collection is changed and the rows applied
        THEN:  a retype is written

        A collection is where a file sits rather than a field an object
        carries, so this is compared against the index, not the model.
        """

        def retype(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                if row["population"] == UNCLASSIFIED:
                    row["collection"] = "transportations"

        outcome = edited(mixed_gaps_trip, retype)

        check.equal(outcome.retyped, 1)
        check.equal(outcome.corrected, 0)

    ####################################################################
    #
    def test_an_unchanged_collection_is_left_alone(
        self, mixed_gaps_trip: str, edited: Callable[..., Outcome]
    ) -> None:
        """
        GIVEN: a retype row nobody edited
        WHEN:  the rows are applied
        THEN:  it reads as already correct rather than as a retype

        The row arrives pre-filled with where the object already is, so
        leaving it alone has to mean leaving it alone.
        """
        outcome = edited(mixed_gaps_trip)

        check.equal(outcome.retyped, 0)
        check.equal(outcome.unchanged, 1, "the unedited retype row")

    ####################################################################
    #
    def test_a_collection_that_does_not_exist_is_refused(
        self, mixed_gaps_trip: str, edited: Callable[..., Outcome]
    ) -> None:
        """
        GIVEN: a retype row naming a collection Tripsy does not have
        WHEN:  the rows are applied
        THEN:  the row is refused, naming what was expected

        Singular and plural are easy to mistype, and the mistake is
        otherwise only found when the retype fails to happen.
        """

        def mistyped(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                if row["population"] == UNCLASSIFIED:
                    row["collection"] = "transportation"

        outcome = edited(mixed_gaps_trip, mistyped)

        check.equal(outcome.retyped, 0)
        check.equal(len(outcome.refused), 1)
        check.is_in("transportations", outcome.refused[0][1])

    ####################################################################
    #
    def test_a_filled_addition_row_becomes_an_addition(
        self, mixed_gaps_trip: str, edited: Callable[..., Outcome]
    ) -> None:
        """
        GIVEN: a row for a record the parser produced nothing for
        WHEN:  its collection is filled in and the rows applied
        THEN:  an addition is written

        An addition row is the only one that arrives with no collection,
        because there is no object to read one off.
        """

        def add(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                if row["population"] == SKIPPED:
                    row["collection"] = "activities"
                    row["fields"]["name"] = "The thing that was skipped"

        outcome = edited(mixed_gaps_trip, add)

        check.equal(outcome.added, 1)

    ####################################################################
    #
    def test_an_addition_row_nobody_filled_in_is_not_an_error(
        self, mixed_gaps_trip: str, edited: Callable[..., Outcome]
    ) -> None:
        """
        GIVEN: an addition row left with no collection
        WHEN:  the rows are applied
        THEN:  it is passed over rather than refused

        A blank collection is the ordinary state of an addition row.
        Refusing it would make every unanswered row an error on every
        run, which would bury the rows that are actually wrong.
        """
        outcome = edited(mixed_gaps_trip)

        check.equal(outcome.added, 0)
        check.equal(outcome.refused, [])
        check.greater_equal(outcome.left_blank, 1)


########################################################################
########################################################################
#
class TestGuessedTimezone:
    """
    Tests for the one population whose rows arrive already filled.

    Only the calendar path produces these: the JSON export says what
    zone a record is in or says nothing, and never guesses.  So these
    stage from an .ics rather than from an export.
    """

    ####################################################################
    #
    def test_the_row_arrives_carrying_the_guess(
        self,
        guessed_timezone_trip: str,
        rows_for: Callable[..., list],
    ) -> None:
        """
        GIVEN: a trip whose event inherited a neighbour's timezone
        WHEN:  the work-list is exported
        THEN:  the row shows the guess rather than a blank

        Every other population arrives empty.  This one has to show what
        was guessed, because overruling a guess means seeing it first.
        """
        rows = rows_for(guessed_timezone_trip)

        row = next(r for r in rows if r["population"] == GUESSED_TIMEZONE)

        check.is_true(row["fields"], "the row offers a field")
        check.is_true(
            any(value for value in row["fields"].values()),
            "and it carries the guess",
        )

    ####################################################################
    #
    def test_overruling_the_guess_writes_a_correction(
        self,
        guessed_timezone_trip: str,
        edited: Callable[..., Outcome],
    ) -> None:
        """
        GIVEN: a work-list row carrying a guessed timezone
        WHEN:  the zone is changed and the rows applied
        THEN:  a correction is written
        """

        def overrule(rows: list[dict[str, Any]]) -> None:
            # Whatever was guessed, say something else -- a fixed zone
            # would silently match the guess and assert nothing.
            #
            for row in rows:
                if row["population"] == GUESSED_TIMEZONE:
                    for name, guess in row["fields"].items():
                        row["fields"][name] = (
                            "Europe/Paris"
                            if guess != "Europe/Paris"
                            else "Asia/Tokyo"
                        )

        outcome = edited(guessed_timezone_trip, overrule)

        check.greater_equal(outcome.corrected, 1)

    ####################################################################
    #
    def test_leaving_the_guess_alone_writes_nothing(
        self,
        guessed_timezone_trip: str,
        edited: Callable[..., Outcome],
    ) -> None:
        """
        GIVEN: a work-list whose guessed-timezone row nobody edited
        WHEN:  the rows are applied
        THEN:  it reads as already correct, not as a correction

        This is the population where a pre-filled value could be written
        straight back as though it were an answer.  Accepting the guess
        has to be free.
        """
        outcome = edited(guessed_timezone_trip)

        check.equal(outcome.corrected, 0)
        check.greater_equal(outcome.unchanged, 1)

    ####################################################################
    #
    def test_a_leg_is_offered_both_of_its_ends(
        self, archive: Archive, unplaceable_trip: str
    ) -> None:
        """
        GIVEN: a guessed-timezone gap against a leg
        WHEN:  the fields for its row are worked out
        THEN:  both ends are offered

        The parser's note does not record which end it guessed, so
        offering one would be wrong half the time.  An activity or a
        stay keeps a single zone and is offered that.
        """
        leg = _legs(composed_children(archive, unplaceable_trip))[0]
        guessed = Gap(
            trip_key=unplaceable_trip,
            population=GUESSED_TIMEZONE,
            uuid="u",
            identifier=str(leg.internal_identifier),
            collection="transportations",
            summary="a leg",
        )

        check.equal(
            field_names(guessed, leg),
            ("departure_timezone", "arrival_timezone"),
        )
        check.equal(field_names(guessed, None), (), "nothing to go on")
