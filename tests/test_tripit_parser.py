#!/usr/bin/env python
#
"""
Test the TripIt GDPR JSON reader against synthetic exports.

The export carries no identifiers and types almost nothing, so most of
what is asserted here is inference: what a record was decided to be, and
what identity was derived for it.  Both are judgement calls a person has
to be able to check afterwards, which is why the notes are tested as
carefully as the objects.
"""

# system imports
import json
from datetime import UTC, datetime
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check
from faker import Faker

# Project imports
from tests import tripit_builder as b
from tripsy_exim.models import is_minted
from tripsy_exim.sources import (
    ACTIVITY,
    HOSTING,
    SKIPPED,
    TRANSPORTATION,
)
from tripsy_exim.sources.ics import uuid_from_uid
from tripsy_exim.sources.tripit import (
    TRIPIT_JSON_NAMESPACE,
    classify,
    load,
    parse_export,
)


####################################################################
#
def only(document: dict[str, Any]) -> Any:
    """Parse an export expected to hold exactly one trip."""
    parsed = parse_export(document)
    assert len(parsed) == 1
    return parsed[0]


########################################################################
########################################################################
#
class TestLoad:
    """Tests for decoding the export."""

    ####################################################################
    #
    def test_text_is_repaired_on_the_way_in(self) -> None:
        """
        GIVEN: an export whose trip name is encoded as the real one is
        WHEN:  it is loaded
        THEN:  the name comes back intact

        A trip name is the join key against the .ics exports, so loading
        it unrecovered produces records that match nothing.
        """
        document = b.export(b.trip(name=b.mojibake("2012年4月 日本の旅行")))

        loaded = load(json.dumps(document))

        assert loaded["Trips"][0]["TripData"]["display_name"] == (
            "2012年4月 日本の旅行"
        )

    ####################################################################
    #
    @pytest.mark.parametrize(
        "text",
        ["not json at all", "[1, 2, 3]", '"a string"'],
        ids=["malformed", "array", "scalar"],
    )
    def test_unusable_input_is_rejected(self, text: str) -> None:
        """
        GIVEN: something that is not an export document
        WHEN:  it is loaded
        THEN:  ValueError is raised rather than a confusing failure later
        """
        with pytest.raises(ValueError):
            load(text)


########################################################################
########################################################################
#
class TestClassify:
    """Tests for deciding what a record is from its shape."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "record,kind,type_value,confident",
        [
            (b.flight(), TRANSPORTATION, "airplane", True),
            (b.rail(), TRANSPORTATION, "train", True),
            (b.rail(train_number=None), TRANSPORTATION, "train", True),
            (b.ground(), TRANSPORTATION, "transfer", True),
            (b.car_rental(), TRANSPORTATION, "car", True),
            (b.ferry(), TRANSPORTATION, "ferry", True),
            (b.lodging(), HOSTING, None, True),
            (b.restaurant(), ACTIVITY, "restaurant", True),
            (b.activity(code="T"), ACTIVITY, "tour", True),
            (b.activity(), ACTIVITY, "general", False),
            (b.directions(), TRANSPORTATION, "roadtrip", False),
        ],
        ids=[
            "flight",
            "rail-with-number",
            "rail-without-number",
            "ground-transport",
            "car-rental",
            "ferry",
            "lodging",
            "restaurant",
            "tour",
            "untyped-activity",
            "directions",
        ],
    )
    def test_records_are_typed_by_shape(
        self,
        record: dict[str, Any],
        kind: str,
        type_value: str | None,
        confident: bool,
    ) -> None:
        """
        GIVEN: a record shaped the way the export shapes that kind
        WHEN:  it is classified
        THEN:  the collection, type and confidence are as expected
        """
        got_kind, got_type, got_confident, reason = classify(record)

        assert (got_kind, got_type, got_confident) == (
            kind,
            type_value,
            confident,
        )
        assert reason

    ####################################################################
    #
    @pytest.mark.parametrize(
        "record,skipped",
        [
            pytest.param(b.map_pin(), True, id="a-map-pin"),
            pytest.param(
                b.activity(name="Map of Santa Barbara, CA", code="T"),
                False,
                id="named-alike-but-shaped-otherwise",
            ),
            pytest.param(
                b.activity(name="Ghibli Museum Visit"),
                False,
                id="shaped-alike-but-named-otherwise",
            ),
        ],
    )
    def test_only_a_map_pin_is_dropped(
        self, record: dict[str, Any], skipped: bool
    ) -> None:
        """
        GIVEN: a record that is or resembles one of TripIt's map pins
        WHEN:  it is classified
        THEN:  only an actual pin is dropped

        Both signals are needed.  The shape alone covers 92 records in a
        real export and 26 of them are plans somebody typed; the name
        alone would drop anything a person chose to call a map.
        """
        kind, _, _, _ = classify(record)

        assert (kind == SKIPPED) is skipped

    ####################################################################
    #
    def test_a_dropped_record_is_reported(self) -> None:
        """
        GIVEN: a trip carrying a map pin
        WHEN:  it is parsed
        THEN:  no object comes out, and the pin is listed as skipped

        A record nobody can see was discarded is indistinguishable from
        one the parser failed to read.
        """
        parsed = only(b.export(b.trip(objects=[b.map_pin(), b.flight()])))

        check.equal(len(parsed.activities), 0, "no object built")
        check.equal(len(parsed.skipped), 1, "and the drop is reported")
        check.is_in("map of a place", parsed.skipped[0].reason)

    ####################################################################
    #
    def test_a_flight_labels_its_ends_with_their_codes(self) -> None:
        """
        GIVEN: a flight segment carrying airport codes
        WHEN:  it is parsed
        THEN:  the codes become the endpoint descriptions, and the name
               is dropped

        The app titles a leg '<departure> to <arrival>' from these, so a
        leg without them reads as ' to '.  TripIt calls every flight
        'Flight', which is not a name anybody chose.
        """
        parsed = only(b.export(b.trip(objects=[b.flight()])))
        leg = parsed.transportations[0]

        check.equal(leg.departure_description, "SAN", "departure code")
        check.equal(leg.arrival_description, "OSA", "arrival code")
        check.is_none(leg.name, "TripIt's generic name dropped")
        check.is_in(
            "Airport", str(leg.departure_address), "address still the place"
        )

    ####################################################################
    #
    def test_a_rail_leg_labels_its_ends_with_the_stations(self) -> None:
        """
        GIVEN: a rail segment naming its stations
        WHEN:  it is parsed
        THEN:  the station names become the endpoint descriptions

        A station has no code to use instead, and the name it is known
        by is what belongs on the row.
        """
        parsed = only(b.export(b.trip(objects=[b.rail()])))
        leg = parsed.transportations[0]

        check.is_in("Shin-Osaka", str(leg.departure_description))
        check.is_in("Okayama", str(leg.arrival_description))

    ####################################################################
    #
    @pytest.mark.parametrize(
        "carrier,expected",
        [
            ("Hakone Ropeway", "train"),
            ("Eizen Cable Car", "train"),
            ("Hakone sightseeing cruise", "cruise"),
            ("Sakurajima Ferry", "ferry"),
            ("Matsue City Bus", "bus"),
            ("Okinawa Airport Shuttle", "bus"),
            ("Narita Express 54", "train"),
            ("JR Uno-port Line", "train"),
            ("Example Operator", None),
        ],
    )
    def test_a_leg_is_typed_by_who_runs_it(
        self, carrier: str, expected: str | None
    ) -> None:
        """
        GIVEN: a transport segment the shape cannot type
        WHEN:  it is parsed
        THEN:  the operator's name settles it, or nothing does

        The shape types most of the corpus; what is left is a cable car,
        a lake cruise, a city bus -- journeys the export records without
        saying what they are.  The operator is the only thing that does.
        """
        parsed = only(
            b.export(b.trip(objects=[b.untyped_transport(carrier=carrier)]))
        )

        assert parsed.transportations[0].transportation_type == expected

    ####################################################################
    #
    @pytest.mark.parametrize(
        "carrier,prefix",
        [
            ("Hakone Ropeway", "Ropeway: "),
            ("Eizen Cable Car", "Funicular: "),
            ("Matsue City Bus", ""),
        ],
    )
    def test_a_mode_tripsy_cannot_draw_goes_in_the_name(
        self, carrier: str, prefix: str
    ) -> None:
        """
        GIVEN: a leg whose mode has no Tripsy type
        WHEN:  it is parsed
        THEN:  the name carries the mode, and the type stays honest

        The icon says rail either way, because that is the nearest thing
        Tripsy draws.  Swapping the type for a real one later leaves the
        name still true.
        """
        parsed = only(
            b.export(b.trip(objects=[b.untyped_transport(carrier=carrier)]))
        )
        leg = parsed.transportations[0]

        check.is_true(str(leg.name).startswith(prefix), f"{leg.name!r}")
        check.is_in(" to ", str(leg.name), "named by its ends")

    ####################################################################
    #
    def test_a_leg_is_named_by_its_ends_and_a_flight_is_not(self) -> None:
        """
        GIVEN: a rail leg and a flight
        WHEN:  they are parsed
        THEN:  the rail leg says where it ran and the flight says nothing

        Tripsy resolves a flight's airport codes and titles the row
        itself -- "San Jose to Santa Barbara" from SJC and SBA -- and it
        does that for flights only.  TripIt's own word for a rail leg is
        "Rail", which says less than the stations do.
        """
        parsed = only(b.export(b.trip(objects=[b.rail(), b.flight()])))
        by_type = {t.transportation_type: t for t in parsed.transportations}

        check.equal(
            by_type["train"].name, "Shin-Osaka Station to Okayama Station"
        )
        check.is_none(by_type["airplane"].name, "left for Tripsy to title")

    ####################################################################
    #
    @pytest.mark.parametrize(
        "record,expected",
        [
            pytest.param(b.rail(), "publicTransport", id="rail"),
            pytest.param(b.ferry(), "publicTransport", id="ferry"),
            pytest.param(b.flight(), None, id="flight"),
            pytest.param(b.car_rental(), None, id="car-rental"),
        ],
    )
    def test_a_station_is_marked_as_public_transport(
        self, record: dict[str, Any], expected: str | None
    ) -> None:
        """
        GIVEN: a leg whose ends are stations, and ones whose are not
        WHEN:  it is parsed
        THEN:  only the former carries the publicTransport category

        A station, stop, terminal or pier is public transport, which is
        what the app sets on the legs it creates.  An airport renders
        without one, and a rental desk is not a stop.
        """
        parsed = only(b.export(b.trip(objects=[record])))
        leg = parsed.transportations[0]

        check.equal(leg.departure_location_type, expected)
        check.equal(leg.arrival_location_type, expected)

    ####################################################################
    #
    def test_rail_is_recognised_without_a_train_number(self) -> None:
        """
        GIVEN: a rail record carrying no train number, as most do
        WHEN:  it is classified
        THEN:  it is still rail

        This is what the JSON settles that the .ics cannot: which
        journeys are trains, without reading prose for the answer.
        """
        kind, _, confident, _ = classify(b.rail(train_number=None))

        assert (kind, confident) == (TRANSPORTATION, True)


########################################################################
########################################################################
#
class TestIdentity:
    """Tests for deriving identity where the export supplies none."""

    ####################################################################
    #
    def test_identifiers_are_stable_across_parses(self) -> None:
        """
        GIVEN: one export
        WHEN:  it is parsed twice
        THEN:  every identifier is identical

        Re-running an import has to be a no-op rather than a duplicate.
        """
        document = b.export(b.trip(objects=[b.flight(), b.lodging()]))

        first, second = only(document), only(document)

        assert first.trip.internal_identifier == second.trip.internal_identifier
        assert [o.internal_identifier for o in first.activities] == [
            o.internal_identifier for o in second.activities
        ]

    ####################################################################
    #
    def test_trips_alike_in_name_and_dates_stay_distinct(self) -> None:
        """
        GIVEN: two trips carrying the same name and the same dates
        WHEN:  the export is parsed
        THEN:  they mint different identifiers

        Identity is derived from content because the export carries no
        ids, and content is not unique: a journey planned twice looks
        identical.  Minting one identifier would make the second POST
        answer an empty 200 and the trip vanish without an error.
        """
        one = b.trip(objects=[b.lodging()])
        two = b.trip(objects=[b.flight()])

        parsed = parse_export(b.export(one, two))

        assert len({p.trip.internal_identifier for p in parsed}) == 2

    ####################################################################
    #
    def test_repeated_objects_within_a_trip_stay_distinct(self) -> None:
        """
        GIVEN: a trip holding two identical records
        WHEN:  it is parsed
        THEN:  each gets its own identifier
        """
        parsed = only(
            b.export(b.trip(objects=[b.restaurant(), b.restaurant()]))
        )

        identifiers = {a.internal_identifier for a in parsed.activities}
        assert len(identifiers) == 2

    ####################################################################
    #
    def test_notes_carry_a_uuid_the_overrides_layer_can_key_on(self) -> None:
        """
        GIVEN: a parsed trip
        WHEN:  its notes are read
        THEN:  each uid is a uuid the archive's index can resolve

        Corrections are keyed by source uuid.  A token the existing
        extractor cannot read would leave every JSON-sourced trip with an
        empty index and no way to apply a correction.
        """
        parsed = only(b.export(b.trip(objects=[b.flight(), b.lodging()])))

        assert parsed.notes
        for note in parsed.notes:
            assert uuid_from_uid(note.uid) == note.uid
            assert is_minted(note.identifier)

    ####################################################################
    #
    def test_identity_does_not_depend_on_how_the_file_was_read(self) -> None:
        """
        GIVEN: an as-exported document, read once raw and once through load
        WHEN:  both are parsed
        THEN:  they mint the same identifiers

        An unrecovered trip name derives a different identifier, and
        Tripsy never releases one, so a caller who skipped recovery
        could not undo the divergence afterwards.  The reader recovers
        trusting that the caller did.
        """
        document = b.export(
            b.trip(
                name=b.mojibake("2012年4月 日本の旅行"), objects=[b.flight()]
            )
        )

        bypassed = only(json.loads(json.dumps(document)))
        through_load = only(load(json.dumps(document)))

        assert bypassed.trip.name == "2012年4月 日本の旅行"
        assert (
            bypassed.trip.internal_identifier
            == through_load.trip.internal_identifier
        )

    ####################################################################
    #
    def test_the_namespace_changes_every_identifier(self) -> None:
        """
        GIVEN: one export parsed into two namespaces
        WHEN:  the identifiers are compared
        THEN:  none is shared, so a shaping run spends none of the real ones
        """
        document = b.export(b.trip(objects=[b.flight()]))

        real = only(document)
        scratch = parse_export(document, "scratch-abc")[0]

        assert real.trip.internal_identifier.startswith(
            f"txim-{TRIPIT_JSON_NAMESPACE}-"
        )
        assert scratch.trip.internal_identifier != real.trip.internal_identifier


########################################################################
########################################################################
#
class TestParsing:
    """Tests for what comes out of a whole export."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "document,trips",
        [
            ({"Trips": []}, 0),
            ({"Trips": [], "screen_name": "example"}, 0),
        ],
    )
    def test_an_account_with_no_trips_parses_to_nothing(
        self, document: dict[str, Any], trips: int
    ) -> None:
        """
        GIVEN: an export whose `Trips` list is empty
        WHEN:  it is parsed
        THEN:  no trips come out, and nothing is raised
        """
        assert len(parse_export(document)) == trips

    ####################################################################
    #
    def test_a_document_without_trips_is_refused(self) -> None:
        """
        GIVEN: a JSON object that is not an export
        WHEN:  it is parsed
        THEN:  ValueError names what is missing

        Parsing it as an empty account would read as a run that imported
        nothing successfully, which is the failure hardest to notice.
        """
        with pytest.raises(ValueError, match="not a TripIt export"):
            parse_export({"something": "else"})

    ####################################################################
    #
    def test_each_leg_becomes_its_own_journey(self) -> None:
        """
        GIVEN: one flight record holding three legs
        WHEN:  it is parsed
        THEN:  three transportations come out, with distinct identifiers

        A record is a booking; a leg is the journey Tripsy models.
        """
        parsed = only(b.export(b.trip(objects=[b.flight(legs=3)])))

        assert len(parsed.transportations) == 3
        assert len({t.internal_identifier for t in parsed.transportations}) == 3

    ####################################################################
    #
    def test_a_flight_leg_is_mapped_end_to_end(self) -> None:
        """
        GIVEN: a flight record
        WHEN:  it is parsed
        THEN:  both endpoints, the carrier and the number are carried over
        """
        parsed = only(b.export(b.trip(objects=[b.flight()])))
        leg = parsed.transportations[0]

        assert leg.transportation_type == "airplane"
        assert leg.company == "Example Air"
        assert leg.transport_number == "100"
        assert leg.departure_latitude == pytest.approx(37.615215)
        assert leg.arrival_address == "Osaka International Airport"
        assert leg.departure_terminal == "I"

    ####################################################################
    #
    def test_offsets_are_preferred_over_zone_names(self) -> None:
        """
        GIVEN: a record whose start carries an offset and a zone
        WHEN:  it is parsed
        THEN:  the instant is read through the offset the export recorded

        The offset is what applied on the day.  A zone's rules can change
        afterwards, so reading a historical instant through the name can
        move it.
        """
        parsed = only(b.export(b.trip(objects=[b.flight()])))
        leg = parsed.transportations[0]

        assert leg.departure_at == datetime(2024, 5, 1, 19, 20, tzinfo=UTC)
        assert leg.departure_timezone == "America/Los_Angeles"

    ####################################################################
    #
    def test_a_record_with_no_time_is_all_day(self) -> None:
        """
        GIVEN: an activity whose DateTime carries a date and no time
        WHEN:  it is parsed
        THEN:  it is marked all-day and read as midnight in its own zone
        """
        parsed = only(b.export(b.trip(objects=[b.activity(all_day=True)])))
        built = parsed.activities[0]

        assert built.all_day is True
        assert built.starts_at == datetime(2024, 5, 2, 15, 0, tzinfo=UTC)

    ####################################################################
    #
    def test_lodging_records_its_all_day_flag_where_it_fits(self) -> None:
        """
        GIVEN: an all-day lodging record, which has no all_day field
        WHEN:  it is parsed
        THEN:  the fact is kept in the passthrough rather than dropped

        The .ics parser uses the same key, so the review does not have to
        know which source a trip came from.
        """
        stay = b.lodging()
        stay["StartDateTime"] = b.moment("2024-05-01", None)

        parsed = only(b.export(b.trip(objects=[stay])))

        assert parsed.hostings[0].source_extras["x_all_day"] == "TRUE"

    ####################################################################
    #
    def test_unmapped_fields_are_retained(self) -> None:
        """
        GIVEN: a record carrying fields the mapping does not read
        WHEN:  it is parsed
        THEN:  they survive in the passthrough, segment fields separated

        Dropping a field on import loses it permanently, which is the
        failure this project exists to answer.
        """
        parsed = only(b.export(b.trip(objects=[b.flight()])))
        extras = parsed.transportations[0].source_extras

        assert extras["is_purchased"] == "true"
        assert extras["segment"]["aircraft"] == "789"

    ####################################################################
    #
    def test_the_trip_itself_is_mapped(self) -> None:
        """
        GIVEN: a trip record
        WHEN:  it is parsed
        THEN:  its name, dates and description come through
        """
        parsed = only(
            b.export(b.trip(description="Visiting friends", objects=[]))
        )

        assert parsed.trip.name == "Osaka, Japan, May 2024"
        assert str(parsed.trip.starts_at) == "2024-05-01"
        assert str(parsed.trip.ends_at) == "2024-05-04"
        assert parsed.trip.description == "Visiting friends"
        assert parsed.trip.source_extras["is_private"] == "false"

    ####################################################################
    #
    def test_unrecognised_records_are_reported_for_review(self) -> None:
        """
        GIVEN: a trip mixing recognised and unrecognised records
        WHEN:  it is parsed
        THEN:  only the unrecognised ones are queued for a person

        Tripsy suppresses duplicates per collection, so an object posted
        into the wrong one cannot be moved afterwards.  Everything the
        reader guessed at has to be visible before an import runs.
        """
        parsed = only(
            b.export(b.trip(objects=[b.flight(), b.activity(), b.directions()]))
        )

        summaries = {n.summary for n in parsed.unclassified}
        assert summaries == {"Example Garden", "Directions from A to B"}

    ####################################################################
    #
    def test_an_empty_export_parses_to_nothing(self) -> None:
        """
        GIVEN: an export carrying no trips
        WHEN:  it is parsed
        THEN:  an empty list comes back rather than an exception
        """
        assert parse_export(b.export()) == []


########################################################################
########################################################################
#
class TestMarkup:
    """Tests for text the export scraped off a page rather than a record."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("<strong>One King Bed</strong><br>", "One King Bed"),
            ("Semi double<br>", "Semi double"),
            (
                '</span> <br> <span class="labelText">Wi-fi Internet</span>',
                "Wi-fi Internet",
            ),
            ("Plain Room", "Plain Room"),
            pytest.param(
                "Wi-fi Internet</span></td&",
                "Wi-fi Internet",
                id="truncated-mid-tag",
            ),
            pytest.param(
                "Room 5 < 10 people",
                "Room 5 < 10 people",
                id="a-bare-angle-bracket-is-not-a-tag",
            ),
        ],
    )
    def test_a_room_description_loses_its_markup(
        self, raw: str, expected: str
    ) -> None:
        """
        GIVEN: a lodging whose room type came off a booking page
        WHEN:  it is parsed
        THEN:  the text survives and the tags do not

        The field is writable, so markup left in it is uploaded verbatim.
        """
        record = b.lodging()
        record["room_type"] = raw

        stay = only(b.export(b.trip(objects=[record]))).hostings[0]

        assert stay.room_type == expected

    ####################################################################
    #
    def test_an_escaped_ampersand_becomes_an_ampersand(self) -> None:
        """
        GIVEN: an address carrying an HTML entity
        WHEN:  it is parsed
        THEN:  the entity is decoded

        'California St &amp; 14th St' is what the export gives, and what
        Tripsy would show unless it is decoded here.
        """
        record = b.ground()
        record["Segment"][0]["StartLocationAddress"] = {
            "address": "California St &amp; 14th St Denver, CO 80202"
        }

        leg = only(b.export(b.trip(objects=[record]))).transportations[0]

        assert (
            leg.departure_address == "California St & 14th St Denver, CO 80202"
        )

    ####################################################################
    #
    def test_a_note_loses_its_tags_and_keeps_its_content(self) -> None:
        """
        GIVEN: a note holding a forwarded email and a stray tag
        WHEN:  it is parsed
        THEN:  the tag goes, and the address and line breaks stay

        An address in angle brackets is content, and a pattern for
        anything bracketed would eat it.  Only named tags are matched,
        which is what makes it safe to clean a note at all.
        """
        record = b.activity()
        record["notes"] = (
            "1 DOUBLE BED<br>\r\n\r\n"
            'From: "no-reply@example.com" <no-reply@example.com>'
        )

        notes = str(
            only(b.export(b.trip(objects=[record]))).activities[0].notes
        )

        check.is_not_in("<br>", notes, "a named tag goes")
        check.is_in("<no-reply@example.com>", notes, "an address stays")
        check.is_in("\n", notes, "the line breaks stay")
        check.is_in("1 DOUBLE BED", notes, "the words stay")


########################################################################
########################################################################
#
class TestInstants:
    """Tests for the times and places a half-recorded leg is read with."""

    ####################################################################
    #
    def segment(self, **fields: Any) -> dict[str, Any]:
        """One ground-transport record holding one segment."""
        return {
            "display_name": "Example",
            "detail_type_code": "G",
            "Segment": [fields],
        }

    ####################################################################
    #
    @pytest.mark.parametrize(
        "end",
        [
            pytest.param(b.moment("2024-05-02", None), id="date-only"),
            pytest.param(None, id="absent"),
        ],
    )
    def test_an_end_the_export_did_not_record_is_not_invented(
        self, end: dict[str, str] | None
    ) -> None:
        """
        GIVEN: a leg whose departure has a time but whose arrival has none
        WHEN:  it is parsed
        THEN:  no arrival comes out

        Midnight in the departure's own zone reads as an arrival hours
        before the departure, which is worse than no arrival at all.
        """
        record = self.segment(StartDateTime=b.moment("2024-05-02", "13:00:00"))
        if end is not None:
            record["Segment"][0]["EndDateTime"] = end

        leg = only(b.export(b.trip(objects=[record]))).transportations[0]

        check.is_not_none(leg.departure_at)
        check.is_none(leg.arrival_at)
        check.is_none(leg.arrival_timezone)

    ####################################################################
    #
    def test_a_day_long_record_keeps_the_day_it_covers(self) -> None:
        """
        GIVEN: an all-day record, dated at both ends and timed at neither
        WHEN:  it is parsed
        THEN:  both instants survive

        An all-day record is a real span, not a missing end.
        """
        stop = only(
            b.export(b.trip(objects=[b.activity(all_day=True)]))
        ).activities[0]

        check.is_not_none(stop.starts_at)
        check.is_not_none(stop.ends_at)

    ####################################################################
    #
    @pytest.mark.parametrize("unplaced", ["StartDateTime", "EndDateTime"])
    def test_an_unplaced_end_is_read_in_the_other_end_s_zone(
        self, unplaced: str
    ) -> None:
        """
        GIVEN: a leg one of whose ends carries neither zone nor offset
        WHEN:  it is parsed
        THEN:  it is read in the zone the other end named, not in UTC

        Both ends belong to one journey.  Reading a Tokyo clock as UTC
        moves it nine hours and puts the arrival before the departure.
        """
        placed = {
            "StartDateTime": b.moment("2024-05-02", "13:00:00"),
            "EndDateTime": b.moment("2024-05-02", "15:00:00"),
        }
        bare = b.moment(
            "2024-05-02",
            placed[unplaced]["time"],
            zone=None,
            offset=None,
        )
        leg = only(
            b.export(
                b.trip(objects=[self.segment(**placed | {unplaced: bare})])
            )
        ).transportations[0]

        check.equal(leg.departure_at, datetime(2024, 5, 2, 4, tzinfo=UTC))
        check.equal(leg.arrival_at, datetime(2024, 5, 2, 6, tzinfo=UTC))
        check.equal(leg.departure_timezone, "Asia/Tokyo")
        check.equal(leg.arrival_timezone, "Asia/Tokyo")

    ####################################################################
    #
    @pytest.mark.parametrize(
        "segment,departure,arrival",
        [
            pytest.param(
                {"location_name": "Togendai-ko"},
                "Togendai-ko",
                "Togendai-ko",
                id="one-bare-place-names-both-ends",
            ),
            pytest.param(
                {
                    "start_location_name": "Gora",
                    "end_location_name": "Sounzan",
                    "location_name": "Togendai-ko",
                },
                "Gora",
                "Sounzan",
                id="a-named-route-wins-over-a-bare-place",
            ),
        ],
    )
    def test_a_stop_recorded_as_one_place_names_both_ends(
        self, segment: dict[str, Any], departure: str, arrival: str
    ) -> None:
        """
        GIVEN: a segment naming a bare place, a route, or both
        WHEN:  it is parsed
        THEN:  a route names its own ends, and a bare place names both

        A ferry or a cruise stop is recorded as a place called at rather
        than a journey between two, and the app draws an unlabelled
        endpoint as nothing at all.
        """
        leg = only(
            b.export(
                b.trip(
                    objects=[
                        self.segment(
                            StartDateTime=b.moment("2024-05-02", "13:00:00"),
                            **segment,
                        )
                    ]
                )
            )
        ).transportations[0]

        check.equal(leg.departure_description, departure)
        check.equal(leg.arrival_description, arrival)


########################################################################
########################################################################
#
class TestGeneratedExports:
    """Tests over whole generated itineraries, TripIt's encoding included."""

    ####################################################################
    #
    def test_a_generated_itinerary_parses(self, faker: Faker) -> None:
        """
        GIVEN: a generated trip -- out, a stay, excursions, and home again
        WHEN:  it is parsed
        THEN:  every record becomes an object and every object a note
        """
        parsed = only(b.export(b.itinerary(faker, excursions=2)))

        objects = (
            len(parsed.hostings)
            + len(parsed.activities)
            + len(parsed.transportations)
        )
        assert objects == len(parsed.notes)
        assert len(parsed.hostings) == 1
        assert parsed.transportations
        assert parsed.activities

    ####################################################################
    #
    @pytest.mark.parametrize("excursions", [0, 1, 3])
    def test_every_object_is_distinctly_identified(
        self, faker: Faker, excursions: int
    ) -> None:
        """
        GIVEN: a trip with excursions that repeat the same shape
        WHEN:  it is parsed
        THEN:  no two objects share an identifier

        Excursions run out and back between the same pair of places, so
        this is where content-derived identity is most likely to collide.
        """
        parsed = only(b.export(b.itinerary(faker, excursions=excursions)))

        identifiers = [
            o.internal_identifier
            for o in (
                *parsed.hostings,
                *parsed.activities,
                *parsed.transportations,
            )
        ]
        assert len(set(identifiers)) == len(identifiers)

    ####################################################################
    #
    def test_generated_text_is_recovered(self, faker: Faker) -> None:
        """
        GIVEN: an export encoded as TripIt encodes, as generated
        WHEN:  it is parsed
        THEN:  no name is left unrecovered

        The generator puts roughly a third of its text through TripIt's
        encoding on purpose.  A suite built only from clean strings
        would pass with the recovery deleted.
        """
        parsed = parse_export(b.random_export(faker, trips=5))

        names = []
        for trip in parsed:
            names.append(trip.trip.name or "")
            for group in (
                trip.hostings,
                trip.activities,
                trip.transportations,
            ):
                names += [o.name or "" for o in group]

        assert names
        for name in names:
            assert "Ã" not in name
            assert "â\x80" not in name

    ####################################################################
    #
    def test_a_generated_export_really_is_encoded(self, faker: Faker) -> None:
        """
        GIVEN: a generated export
        WHEN:  its raw text is inspected
        THEN:  some of it really did go through TripIt's encoding

        Guards the test above: if the generator stopped producing
        any, that test would pass for the wrong reason.
        """
        raw = json.dumps(b.random_export(faker, trips=5), ensure_ascii=False)

        assert "Ã" in raw or "â" in raw or "Å" in raw
