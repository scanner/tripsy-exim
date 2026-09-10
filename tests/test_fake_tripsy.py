#!/usr/bin/env python
#
"""
Test that the fake behaves the way the documented API does.

The fake is what every later layer is developed against, so a wrong fake
would certify a broken client.  Five of these were verified against the
live API on 2026-09-09; the expense-permission one is modelled from the
documentation and cannot be observed, because the account is premium.
"""

# system imports
from datetime import timedelta
from decimal import Decimal
from typing import Any

# 3rd party imports
import httpx
import pytest
import pytest_check as check

# Project imports
from tests.fake_tripsy import BASE, PAGE_SIZE, FakeTripsy, transport
from tripsy_exim.models import Hosting, Trip
from tripsy_exim.store import Archive


####################################################################
#
def client_for(store: FakeTripsy) -> httpx.Client:
    """An httpx client wired to a particular store."""
    return httpx.Client(base_url=BASE, transport=transport(store))


########################################################################
########################################################################
#
class TestEnvelopes:
    """Tests the two list shapes the API uses."""

    ####################################################################
    #
    def test_v1_trips_is_the_envelope_exception(
        self, tripsy_client: httpx.Client
    ) -> None:
        """
        GIVEN: trips in the store
        WHEN:  each list route is fetched
        THEN:  /v1/trips returns a bare results key and v2 paginates, so a
               client that handles only one shape fails here
        """
        tripsy_client.post("/v1/trips", json={"name": "Example Trip"})

        v1 = tripsy_client.get("/v1/trips").json()
        v2 = tripsy_client.get("/v2/trips").json()

        check.equal(sorted(v1.keys()), ["results"], "v1 is bare")
        check.equal(
            sorted(v2.keys()),
            ["count", "next", "previous", "results"],
            "v2 paginates",
        )

    ####################################################################
    #
    def test_pagination_triggers_at_the_real_trip_size(
        self, fake_tripsy: FakeTripsy
    ) -> None:
        """
        GIVEN: a trip with 121 children, the largest in the real export
        WHEN:  its children are listed
        THEN:  the first page holds 100 and a next link is offered, so
               paging is exercised on the first import rather than in
               theory
        """
        trip = fake_tripsy.seed_trip(name="Example Trip")
        for index in range(121):
            fake_tripsy.seed_child(
                trip["id"], "activities", name=f"Activity {index}"
            )

        with client_for(fake_tripsy) as client:
            first = client.get(f"/v2/trip/{trip['id']}/activities").json()
            second = client.get(
                f"/v2/trip/{trip['id']}/activities", params={"page": 2}
            ).json()

        check.equal(first["count"], 121, "count is the total, not the page")
        check.equal(len(first["results"]), PAGE_SIZE, "first page full")
        check.is_not_none(first["next"], "next offered")
        check.equal(len(second["results"]), 21, "remainder on page two")
        check.is_none(second["next"], "last page")


########################################################################
########################################################################
#
class TestIdempotency:
    """Tests duplicate suppression, the thing that makes import re-runnable."""

    ####################################################################
    #
    def test_a_duplicate_trip_identifier_creates_nothing(
        self, tripsy_client: httpx.Client
    ) -> None:
        """
        GIVEN: a trip already created with an identifier over 5 characters
        WHEN:  the same payload is posted again
        THEN:  an empty 200 comes back and no second trip exists
        """
        payload = {"internal_identifier": "txim-ics-abcdef", "name": "Trip"}
        first = tripsy_client.post("/v1/trips", json=payload)

        second = tripsy_client.post("/v1/trips", json=payload)

        check.equal(first.status_code, 201, "first creates")
        check.equal(second.status_code, 200, "second suppressed")
        check.equal(len(second.content), 0, "and carries no body")
        check.equal(
            len(tripsy_client.get("/v1/trips").json()["results"]), 1, "one trip"
        )

    ####################################################################
    #
    def test_a_short_identifier_does_not_suppress(
        self, tripsy_client: httpx.Client
    ) -> None:
        """
        GIVEN: a trip identifier of 5 characters or fewer
        WHEN:  the same payload is posted twice
        THEN:  a second trip is created, since suppression needs length --
               which is why minted identifiers are far longer
        """
        payload = {"internal_identifier": "short", "name": "Trip"}
        tripsy_client.post("/v1/trips", json=payload)

        second = tripsy_client.post("/v1/trips", json=payload)

        assert second.status_code == 201

    ####################################################################
    #
    def test_a_duplicate_child_identifier_creates_nothing(
        self, tripsy_client: httpx.Client
    ) -> None:
        """
        GIVEN: a hosting already created in a trip
        WHEN:  the same identifier is posted to that trip again
        THEN:  an empty 200 comes back, with no length threshold
        """
        trip = tripsy_client.post("/v1/trips", json={"name": "T"}).json()
        payload = {"internal_identifier": "h1", "name": "Lodging"}
        tripsy_client.post(f"/v1/trip/{trip['id']}/hostings", json=payload)

        second = tripsy_client.post(
            f"/v1/trip/{trip['id']}/hostings", json=payload
        )

        check.equal(second.status_code, 200, "suppressed")
        check.equal(len(second.content), 0, "empty body")


########################################################################
########################################################################
#
class TestUpdatedSince:
    """Tests the incremental export filter."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "offset_days,expected",
        [
            pytest.param(1, 1, id="plus-1-day-still-returned"),
            pytest.param(3, 0, id="plus-3-days-filtered-out"),
        ],
    )
    def test_the_two_day_cushion_is_applied(
        self, fake_tripsy: FakeTripsy, offset_days: int, expected: int
    ) -> None:
        """
        GIVEN: a trip modified now
        WHEN:  updatedSince is set in the future
        THEN:  +1 day still returns it and +3 does not, because the server
               subtracts two days before filtering
        """
        fake_tripsy.seed_trip(name="Example Trip")
        future = fake_tripsy.now + timedelta(days=offset_days)
        since = future.strftime("%Y-%m-%dT%H:%M:%SZ")

        with client_for(fake_tripsy) as client:
            body = client.get(
                "/v2/trips", params={"updatedSince": since}
            ).json()

        assert body["count"] == expected

    ####################################################################
    #
    def test_a_nested_write_bumps_the_parent_trip(
        self, fake_tripsy: FakeTripsy
    ) -> None:
        """
        GIVEN: a trip untouched for a week whose hosting is then edited
        WHEN:  trips are listed with updatedSince after the trip's own edit
        THEN:  the trip comes back, which is why the incremental pass is
               sufficient on its own
        """
        trip = fake_tripsy.seed_trip(name="Example Trip")
        fake_tripsy.advance(days=7)
        watermark = fake_tripsy.now.strftime("%Y-%m-%dT%H:%M:%SZ")
        fake_tripsy.advance(days=1)
        fake_tripsy.seed_child(trip["id"], "hostings", name="Lodging")

        with client_for(fake_tripsy) as client:
            body = client.get(
                "/v2/trips", params={"updatedSince": watermark}
            ).json()

        assert body["count"] == 1


########################################################################
########################################################################
#
class TestDeletion:
    """Tests the soft-delete tombstone the exporter's deletion pass reads."""

    ####################################################################
    #
    def test_a_deleted_trip_becomes_an_id_only_tombstone(
        self, tripsy_client: httpx.Client
    ) -> None:
        """
        GIVEN: a trip that is then deleted
        WHEN:  active and deleted listings are fetched
        THEN:  it leaves the active list and appears under deleted=true
               carrying nothing but its id
        """
        trip = tripsy_client.post("/v1/trips", json={"name": "T"}).json()

        removed = tripsy_client.delete(f"/v1/trips/{trip['id']}")
        active = tripsy_client.get("/v2/trips").json()
        tombs = tripsy_client.get(
            "/v2/trips", params={"deleted": "true"}
        ).json()

        check.equal(removed.status_code, 204, "no content")
        check.equal(active["count"], 0, "gone from active")
        check.equal(tombs["results"], [{"id": trip["id"]}], "id only")


########################################################################
########################################################################
#
class TestPermissionsAndFields:
    """Tests the two ways a response arrives partial."""

    ####################################################################
    #
    def test_expenses_are_withheld_without_permission(
        self, restricted_tripsy: FakeTripsy
    ) -> None:
        """
        GIVEN: a caller who cannot see expenses
        WHEN:  a hosting carrying a price is fetched
        THEN:  price and currency are absent rather than null, which is
               the case merge-on-set exists to survive
        """
        trip = restricted_tripsy.seed_trip(name="T")
        restricted_tripsy.seed_child(
            trip["id"], "hostings", name="Lodging", price=78.5, currency="EUR"
        )

        with client_for(restricted_tripsy) as client:
            body = client.get(f"/v2/trip/{trip['id']}/hostings").json()

        hosting = body["results"][0]
        check.is_not_in("price", hosting, "price withheld")
        check.is_not_in("currency", hosting, "currency withheld")
        check.is_in("name", hosting, "everything else still there")

    ####################################################################
    #
    @pytest.mark.parametrize(
        "params,present,absent",
        [
            pytest.param(
                {"fields": "id,internal_identifier"},
                "internal_identifier",
                "name",
                id="fields-includes",
            ),
            pytest.param(
                {"fields!": "name"}, "id", "name", id="fields-bang-excludes"
            ),
        ],
    )
    def test_field_selection_returns_partial_objects(
        self,
        tripsy_client: httpx.Client,
        params: dict[str, str],
        present: str,
        absent: str,
    ) -> None:
        """
        GIVEN: a trip in the store
        WHEN:  it is fetched with fields= or fields!=
        THEN:  the response is partial by construction
        """
        tripsy_client.post(
            "/v1/trips", json={"internal_identifier": "txim-x", "name": "T"}
        )

        results = tripsy_client.get("/v1/trips", params=params).json()[
            "results"
        ]

        check.is_in(present, results[0], f"{present} kept")
        check.is_not_in(absent, results[0], f"{absent} dropped")


########################################################################
########################################################################
#
class TestPartialFetchesAgainstTheArchive:
    """
    The reason the fake and the archive both exist.

    A lean fetch followed by a full one must accumulate rather than
    overwrite, and only something that behaves like a server exercises
    partial responses, merge-on-set and exclude_unset together.
    """

    ####################################################################
    #
    def test_a_lean_fetch_then_a_full_one_loses_nothing(
        self, fake_tripsy: FakeTripsy, archive: Archive
    ) -> None:
        """
        GIVEN: a trip archived from a fields= response carrying two fields
        WHEN:  the full object is fetched and archived over it
        THEN:  the archive holds the union, with nothing blanked
        """
        seeded = fake_tripsy.seed_trip(
            internal_identifier="txim-ics-abcdef",
            name="Example Trip",
            timezone="Europe/Rome",
            description="Synthetic",
        )

        with client_for(fake_tripsy) as client:
            lean = client.get(
                "/v1/trips", params={"fields": "id,internal_identifier"}
            ).json()["results"][0]
            archive.ingest(Trip, lean)

            full = client.get(f"/v1/trips/{seeded['id']}").json()
            archive.ingest(Trip, full)

        stored = archive.read(
            Trip, archive.trip_dir("txim-ics-abcdef") / "trip.json"
        )
        assert stored is not None

        check.equal(stored.name, "Example Trip", "gained from the full fetch")
        check.equal(stored.timezone, "Europe/Rome", "gained")
        check.equal(stored.id, seeded["id"], "kept from the lean fetch")

    ####################################################################
    #
    def test_a_restricted_fetch_cannot_erase_an_archived_price(
        self, archive: Archive
    ) -> None:
        """
        GIVEN: a hosting archived with a price
        WHEN:  a later export by a caller without expense permission runs
        THEN:  the archived price survives, because the field is absent
               rather than null
        """
        full = FakeTripsy()
        trip = full.seed_trip(name="T")
        full.seed_child(
            trip["id"],
            "hostings",
            internal_identifier="txim-ics-h1",
            name="Lodging",
            price=78.5,
            currency="EUR",
        )
        with client_for(full) as client:
            body = client.get(f"/v2/trip/{trip['id']}/hostings").json()
            archive.ingest(Hosting, body["results"][0], trip_key="t")

        # The same store, now seen by a caller who cannot see expenses.
        #
        full.can_see_expenses = False
        with client_for(full) as client:
            body = client.get(f"/v2/trip/{trip['id']}/hostings").json()
            restricted: dict[str, Any] = body["results"][0]
            archive.ingest(Hosting, restricted, trip_key="t")

        stored = archive.read(
            Hosting, archive.trip_dir("t") / "hostings" / "txim-ics-h1.json"
        )
        assert stored is not None

        check.is_not_in("price", restricted, "the fetch really was restricted")
        check.equal(stored.price, Decimal("78.5"), "archived price survives")
        check.equal(stored.currency, "EUR", "and its currency")
