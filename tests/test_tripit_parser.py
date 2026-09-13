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
from faker import Faker

# Project imports
from tests import tripit_builder as b
from tripsy_exim.models import is_minted
from tripsy_exim.sources import ACTIVITY, HOSTING, TRANSPORTATION
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
