#!/usr/bin/env python
#
"""
Test the API client against the in-memory Tripsy.

Two things are being checked here and they are worth telling apart.  The
route tests assert behaviour verified against the live API on 2026-09-09 --
duplicate suppression, the bare `/v1/trips` envelope, soft deletes.  The
throttling tests assert only what this client does when a service tells it
to slow down; Tripsy has never been seen to do so, and the fake's 429s are
a design decision rather than an observation.

Time is injected, so a test can watch a run back off for two minutes
without taking two minutes.
"""

# 3rd party imports
import httpx
import pytest
import pytest_check as check

# Project imports
from tests.clock import FakeClock
from tests.conftest import client_for, paced_store
from tests.fake_tripsy import BASE, FakeTripsy, transport
from tripsy_exim.api import (
    BACKUP,
    IMPORT,
    INTERACTIVE,
    AlreadyExists,
    APIError,
    AuthenticationError,
    BadRequest,
    BearerAuth,
    Created,
    MethodNotAllowed,
    NotFound,
    Pacer,
    PermissionDenied,
    RateLimited,
    RetryPolicy,
    ServerError,
    TokenAuth,
    TransportError,
    TripsyClient,
)
from tripsy_exim.api.client import MIN_TRIP_IDENTIFIER_LENGTH
from tripsy_exim.api.retry import DEFAULT as DEFAULT_RETRIES

# Long enough for trip-level duplicate suppression to engage.
#
IDENT = "txim-ics-0123456789abcdef"


########################################################################
########################################################################
#
class TestPacingEndToEnd:
    """Tests that the pace reaches the wire, not just the pacer."""

    ####################################################################
    #
    def test_a_slow_api_slows_the_run_down(self, clock: FakeClock) -> None:
        """
        GIVEN: an API taking two seconds to answer
        WHEN:  three calls are made through the client
        THEN:  each one after the first waits about as long as the API
               took, so load on the service is halved rather than
               ignored
        """
        store = paced_store(clock, latency=2.0)
        with client_for(store, clock, profile=IMPORT) as client:
            for _ in range(3):
                client.list_trips_v1()

            check.equal(
                [round(s, 3) for s in clock.slept],
                [2.0, 2.0],
                "paced to the observed latency",
            )
            check.equal(
                client.pacer.delay, pytest.approx(2.0), "and stays there"
            )

    ####################################################################
    #
    def test_a_fast_api_is_paced_anyway(self, clock: FakeClock) -> None:
        """
        GIVEN: an API answering instantly
        WHEN:  calls are made
        THEN:  they are still separated by the profile's floor, because
               an unpaced run is the one that finds the limit
        """
        store = paced_store(clock, latency=0.0)
        with client_for(store, clock, profile=IMPORT) as client:
            client.list_trips_v1()
            client.list_trips_v1()

        assert clock.slept == [pytest.approx(IMPORT.min_delay)]

    ####################################################################
    #
    def test_one_pacer_paces_every_client_sharing_it(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: two clients handed the same pacer
        WHEN:  each makes a call
        THEN:  the second waits for the first, because a session is
               defined by the pacer and not by the client object
        """
        pacer = Pacer(IMPORT)
        store = paced_store(clock, latency=1.0)

        with client_for(store, clock, pacer=pacer) as first:
            with client_for(store, clock, pacer=pacer) as second:
                first.list_trips_v1()
                second.list_trips_v1()

        check.equal(clock.slept, [pytest.approx(1.0)], "the second waited")
        check.equal(pacer.requests, 2, "both went through one pacer")

    ####################################################################
    #
    def test_the_backup_profile_is_politer_than_the_import_one(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: the same API latency under each profile
        WHEN:  a call is made
        THEN:  a monthly backup paces itself well below an import, since
               nothing is waiting on it finishing sooner
        """
        gaps = {}
        for profile in (IMPORT, BACKUP):
            store = paced_store(clock, latency=0.5)
            with client_for(store, clock, profile=profile) as client:
                client.list_trips_v1()
                gaps[profile.name] = client.pacer.delay

        check.equal(gaps["import"], pytest.approx(0.5), "import keeps up")
        check.equal(gaps["backup"], pytest.approx(1.0), "backup hangs back")


########################################################################
########################################################################
#
class TestThrottleHandling:
    """
    Tests the response to being told to slow down.

    Speculative throughout: Tripsy sends no rate-limit headers and no
    throttle has been observed from it.  These assert what the client
    does when told, not what the service says.
    """

    ####################################################################
    #
    def test_a_throttled_write_is_sent_again_and_succeeds(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a create whose first attempt is refused with a Retry-After
        WHEN:  the client sends it
        THEN:  it waits out the directive, re-sends, and the caller sees
               only the eventual success
        """
        store = paced_store(clock)
        store.throttle(count=1, retry_after=30)

        with client_for(store, clock, profile=IMPORT) as client:
            result = client.create_trip(
                {"name": "Example", "internal_identifier": IDENT}
            )

            check.is_instance(result, Created, "the create landed")
            check.equal(
                [m for m, _ in store.requests],
                ["POST", "POST"],
                "it was actually re-sent",
            )
            check.greater_equal(
                max(clock.slept), 30.0, "and waited what it was told"
            )

    ####################################################################
    #
    def test_a_directive_paces_the_rest_of_the_session(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a throttle carrying a Retry-After
        WHEN:  later unrelated calls are made
        THEN:  the directive has become the floor for those too, and
               decays back only as they succeed
        """
        store = paced_store(clock)
        store.throttle(count=1, retry_after=8)

        with client_for(store, clock, profile=IMPORT) as client:
            client.list_trips_v1()
            check.equal(
                client.pacer.delay, pytest.approx(4.0), "floor halved once"
            )

            client.list_trips_v1()
            check.equal(
                client.pacer.delay, pytest.approx(2.0), "and halves again"
            )

    ####################################################################
    #
    def test_a_directive_longer_than_the_profile_allows_is_refused(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a throttle asking for ten minutes
        WHEN:  the client sees it
        THEN:  it raises rather than parking the run, because a header
               that long cannot be sanity-checked and an unattended
               backup would simply stop
        """
        store = paced_store(clock)
        store.throttle(count=1, retry_after=600)

        with client_for(store, clock, profile=IMPORT) as client:
            with pytest.raises(RateLimited) as raised:
                client.list_trips_v1()

        check.equal(raised.value.retry_after, 600.0, "reported as asked")
        check.equal(len(store.requests), 1, "not re-sent")
        check.equal(clock.slept, [], "and nothing was waited out")

    ####################################################################
    #
    def test_retries_are_finite(self, clock: FakeClock) -> None:
        """
        GIVEN: an API that refuses everything
        WHEN:  a call is made
        THEN:  the client gives up after the policy's attempts rather
               than retrying forever
        """
        store = paced_store(clock)
        store.throttle(count=20, retry_after=1)

        with client_for(store, clock, profile=IMPORT) as client:
            with pytest.raises(RateLimited):
                client.list_trips_v1()

            check.equal(
                len(store.requests),
                client.retries.max_attempts,
                "attempts are bounded",
            )

    ####################################################################
    #
    @pytest.mark.parametrize(
        "payload,expected_sends",
        [
            # A minted identifier makes a repeat a no-op, so the create
            # can safely be sent again.
            ({"name": "Example", "internal_identifier": IDENT}, 2),
            # Without one, a lost response really could mean a duplicate
            # object, so it is not repeated.
            ({"name": "Example"}, 1),
        ],
    )
    def test_a_create_is_retried_only_when_it_is_idempotent(
        self,
        clock: FakeClock,
        payload: dict[str, str],
        expected_sends: int,
    ) -> None:
        """
        GIVEN: a create that is refused once
        WHEN:  the payload does or does not carry an internal_identifier
        THEN:  it is repeated only in the case where a repeat cannot
               create a second object
        """
        store = paced_store(clock)
        store.throttle(count=1, retry_after=1)

        with client_for(store, clock, profile=IMPORT) as client:
            try:
                client.create_trip(payload)
            except RateLimited:
                pass

        assert len(store.requests) == expected_sends

    ####################################################################
    #
    def test_a_refused_connection_backs_the_run_off(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a transport that fails instantly rather than slowly
        WHEN:  the client gives up on it
        THEN:  the pace has still been backed off, since a fast failure
               would otherwise look like the quickest response of the run
        """

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        pacer = Pacer(IMPORT)
        with TripsyClient(
            base_url=BASE,
            transport=httpx.MockTransport(refuse),
            pacer=pacer,
            retries=RetryPolicy(jitter=0.0),
        ) as client:
            with pytest.raises(TransportError):
                client.list_trips_v1()

        check.greater(pacer.delay, IMPORT.min_delay, "backed off")
        check.equal(pacer.failures, 4, "every failure counted")
        check.equal(pacer.throttles, 0, "and none of them called a throttle")


########################################################################
########################################################################
#
class TestWrites:
    """Tests the v1 write surface and duplicate suppression."""

    ####################################################################
    #
    def test_a_repeated_create_reports_that_nothing_was_written(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a trip already created with a minted identifier
        WHEN:  the same create is sent again, as a re-run of an import
               would
        THEN:  the second answers AlreadyExists rather than creating a
               duplicate, which is what makes an import re-runnable
        """
        payload = {"name": "Example", "internal_identifier": IDENT}

        first = api_client.create_trip(payload)
        second = api_client.create_trip(payload)

        check.is_instance(first, Created, "the first landed")
        check.is_instance(second, AlreadyExists, "the second did not")
        assert isinstance(second, AlreadyExists)
        check.equal(second.internal_identifier, IDENT, "and says which one")

    ####################################################################
    #
    def test_an_identifier_resolves_to_an_id_in_one_call(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: trips created earlier, whose ids the caller did not keep
        WHEN:  the identifier map is fetched
        THEN:  one lean request maps every identifier to its id, so
               nothing has to be cached for correctness
        """
        created = api_client.create_trip(
            {"name": "Example", "internal_identifier": IDENT}
        )
        assert isinstance(created, Created)

        mapping = api_client.trip_ids_by_identifier()

        assert mapping[IDENT] == created.payload["id"]

    ####################################################################
    #
    def test_a_child_create_and_update_round_trips(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a trip
        WHEN:  a hosting is created under it and then patched
        THEN:  the update reaches the object and comes back changed
        """
        trip = api_client.create_trip({"name": "Example"})
        assert isinstance(trip, Created)
        trip_id = trip.payload["id"]

        child = api_client.create_child(
            trip_id, "hostings", {"name": "Somewhere"}
        )
        assert isinstance(child, Created)

        updated = api_client.update_child(
            trip_id, "hostings", child.payload["id"], {"name": "Elsewhere"}
        )

        assert updated["name"] == "Elsewhere"

    ####################################################################
    #
    def test_an_update_cannot_move_an_object_by_accident(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: an update payload that still carries update_trip, as one
               built from a fetched object would
        WHEN:  it is sent as an update
        THEN:  the client refuses before the request goes out, because
               the API would move the object and answer an empty 200
               that nothing above the transport could tell from success
        """
        with pytest.raises(ValueError, match="move_child"):
            api_client.update_child(1, "hostings", 2, {"update_trip": 9})

    ####################################################################
    #
    def test_moving_an_object_on_purpose_works(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a hosting on one trip and a second trip to move it to
        WHEN:  move_child is called
        THEN:  the object is found under the destination trip
        """
        source = api_client.create_trip({"name": "Source"})
        target = api_client.create_trip({"name": "Target"})
        assert isinstance(source, Created) and isinstance(target, Created)

        child = api_client.create_child(
            source.payload["id"], "hostings", {"name": "Somewhere"}
        )
        assert isinstance(child, Created)

        api_client.move_child(
            source.payload["id"],
            "hostings",
            child.payload["id"],
            target.payload["id"],
        )

        moved = list(api_client.iter_children(target.payload["id"], "hostings"))
        assert [c["id"] for c in moved] == [child.payload["id"]]

    ####################################################################
    #
    def test_a_deleted_trip_leaves_a_tombstone(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a trip that is deleted
        WHEN:  the deleted list is read
        THEN:  it appears as an id and nothing else, which is the only
               way an exporter learns of a deletion
        """
        trip = api_client.create_trip({"name": "Example"})
        assert isinstance(trip, Created)

        api_client.delete_trip(trip.payload["id"])
        tombstones = list(api_client.iter_trips(deleted=True))

        assert tombstones == [{"id": trip.payload["id"]}]

    ####################################################################
    #
    def test_an_unknown_collection_is_caught_before_the_request(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a misspelled collection name
        WHEN:  a child route is called with it
        THEN:  it raises rather than becoming a 404 from a route that
               looks real
        """
        with pytest.raises(ValueError, match="unknown collection"):
            api_client.create_child(1, "hostels", {})


########################################################################
########################################################################
#
class TestReads:
    """Tests the read surface, its two envelopes, and paging."""

    ####################################################################
    #
    def test_v1_trips_has_no_pages_and_v2_does(self, clock: FakeClock) -> None:
        """
        GIVEN: more trips than fit on one page
        WHEN:  each list route is read
        THEN:  v1 answers all of them in one bare response and v2 pages,
               so a client handling only one shape would fail here
        """
        store = paced_store(clock, page_size=5)
        for index in range(12):
            store.seed_trip(name=f"Trip {index}")

        with client_for(store, clock) as client:
            flat = client.list_trips_v1()
            paged = list(client.iter_trips())

        check.equal(len(flat), 12, "v1 returns everything at once")
        check.equal(len(paged), 12, "v2 returns everything across pages")

    ####################################################################
    #
    def test_a_filter_survives_to_the_last_page(self, clock: FakeClock) -> None:
        """
        GIVEN: a field-limited read spanning several pages
        WHEN:  the pages are followed
        THEN:  every page is still field-limited, because the client
               follows the link the server built rather than reissuing a
               bare page request
        """
        store = paced_store(clock, page_size=5)
        for index in range(12):
            store.seed_trip(name=f"Trip {index}")

        with client_for(store, clock) as client:
            trips = list(client.iter_trips(fields=["id"]))

        check.equal(len(trips), 12, "all pages arrived")
        check.equal(
            {key for trip in trips for key in trip},
            {"id"},
            "and none of them widened",
        )

    ####################################################################
    #
    def test_a_child_collection_pages(self, clock: FakeClock) -> None:
        """
        GIVEN: a trip with more children than fit on a page
        WHEN:  the collection is iterated
        THEN:  every child arrives, which a real trip's 121 events makes
               the ordinary case rather than an edge one
        """
        store = paced_store(clock, page_size=5)
        trip = store.seed_trip(name="Example")
        for index in range(12):
            store.seed_child(trip["id"], "activities", name=f"Act {index}")

        with client_for(store, clock) as client:
            children = list(client.iter_children(trip["id"], "activities"))

        assert len(children) == 12

    ####################################################################
    #
    def test_a_lean_fetch_returns_only_what_was_asked_for(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a trip with many fields set
        WHEN:  it is fetched with a field restriction
        THEN:  only those fields come back, which is what makes every
               response potentially partial
        """
        created = api_client.create_trip(
            {"name": "Example", "internal_identifier": IDENT}
        )
        assert isinstance(created, Created)

        lean = api_client.get_trip(created.payload["id"], fields=["id", "name"])

        assert sorted(lean) == ["id", "name"]


########################################################################
########################################################################
#
class TestErrorMapping:
    """Tests that statuses become the distinctions callers act on."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "status,expected",
        [
            (400, BadRequest),
            (401, AuthenticationError),
            (403, PermissionDenied),
            (404, NotFound),
            (405, MethodNotAllowed),
            (500, ServerError),
        ],
    )
    def test_a_status_becomes_its_own_exception(
        self, clock: FakeClock, status: int, expected: type[APIError]
    ) -> None:
        """
        GIVEN: a response with an unsuccessful status
        WHEN:  the client reads it
        THEN:  it raises the class standing for that status, so nothing
               above this layer has to know any status codes
        """
        store = paced_store(clock)
        store.throttle(count=1, status=status, body={"detail": "nope"})

        with client_for(store, clock) as client:
            with pytest.raises(expected) as raised:
                client.list_trips_v1()

        check.equal(raised.value.status_code, status, "status is kept")
        check.is_in("nope", str(raised.value), "and so is the detail")

    ####################################################################
    #
    def test_a_field_error_names_the_field(self, clock: FakeClock) -> None:
        """
        GIVEN: a rejected payload reported as DRF field errors
        WHEN:  it is raised
        THEN:  the message names the field, which after 1300 writes is
               the only part worth putting in front of a person
        """
        store = paced_store(clock)
        store.throttle(
            count=1, status=400, body={"starts_at": ["Invalid date."]}
        )

        with client_for(store, clock) as client:
            with pytest.raises(BadRequest) as raised:
                client.list_trips_v1()

        check.is_in("starts_at", str(raised.value), "names the field")
        check.is_in("Invalid date.", str(raised.value), "and the reason")

    ####################################################################
    #
    def test_a_missing_trip_is_a_not_found(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a trip id that does not exist
        WHEN:  it is fetched
        THEN:  NotFound is raised, from the fake's own routing rather
               than an injected status
        """
        with pytest.raises(NotFound):
            api_client.get_trip(9999)

    ####################################################################
    #
    def test_a_server_error_is_not_retried(self, clock: FakeClock) -> None:
        """
        GIVEN: a 500, which from a DRF service means an unhandled
               exception
        WHEN:  a call meets one
        THEN:  it is raised at once, since an identical second request
               reproduces the bug rather than surviving it
        """
        store = paced_store(clock)
        store.throttle(count=5, status=500, body={"detail": "boom"})

        with client_for(store, clock) as client:
            with pytest.raises(ServerError):
                client.list_trips_v1()

        assert len(store.requests) == 1


########################################################################
########################################################################
#
class TestAuthentication:
    """Tests the two header schemes and the login exchange."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "auth,expected",
        [
            (TokenAuth("abc"), "Token abc"),
            (BearerAuth("abc"), "Bearer abc"),
        ],
    )
    def test_each_scheme_sends_its_own_prefix(
        self, auth: TokenAuth | BearerAuth, expected: str
    ) -> None:
        """
        GIVEN: a token under each of the two schemes
        WHEN:  the header is built
        THEN:  the prefixes differ, which is the whole reason these are
               separate classes -- confusing them yields the same 401
        """
        assert auth.header == expected

    ####################################################################
    #
    def test_logging_in_stores_the_token_on_the_client(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a client with credentials but no useful token
        WHEN:  it logs in
        THEN:  the token it gets back is the one it goes on to send, so
               nothing has to be re-wired to authenticate
        """
        token = api_client.login("someone", "secret")

        check.equal(token, "fake-token-for-tests", "the token came back")
        check.equal(api_client.auth.token, token, "and is now in use")

    ####################################################################
    #
    def test_an_unauthenticated_client_still_sends_the_login(
        self, clock: FakeClock, paced_tripsy: FakeTripsy
    ) -> None:
        """
        GIVEN: a client holding no token at all
        WHEN:  it calls the route that issues tokens
        THEN:  the request goes out, rather than being blocked by its
               own precondition
        """
        with TripsyClient(
            base_url=BASE,
            transport=transport(paced_tripsy),
        ) as client:
            assert client.login("someone", "secret")


########################################################################
########################################################################
#
class TestRemainingRoutes:
    """Tests the routes the other classes did not happen to exercise."""

    ####################################################################
    #
    def test_a_trip_update_sends_only_what_changed(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: an existing trip
        WHEN:  one field is patched
        THEN:  that field changes and the others are left alone, which
               is what makes a partial model safe to write
        """
        created = api_client.create_trip(
            {"name": "Example", "description": "Original"}
        )
        assert isinstance(created, Created)

        updated = api_client.update_trip(
            created.payload["id"], {"name": "Renamed"}
        )

        check.equal(updated["name"], "Renamed", "the field changed")
        check.equal(
            updated["description"], "Original", "the rest was untouched"
        )

    ####################################################################
    #
    def test_a_child_can_be_fetched_and_deleted(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a hosting on a trip
        WHEN:  it is fetched by id and then deleted
        THEN:  the fetch returns it and the delete leaves a tombstone,
               since Tripsy deletes softly
        """
        trip = api_client.create_trip({"name": "Example"})
        assert isinstance(trip, Created)
        trip_id = trip.payload["id"]

        child = api_client.create_child(
            trip_id, "hostings", {"name": "Somewhere"}
        )
        assert isinstance(child, Created)
        child_id = child.payload["id"]

        fetched = api_client.get_child(trip_id, "hostings", child_id)
        check.equal(fetched["name"], "Somewhere", "fetched by id")

        api_client.delete_child(trip_id, "hostings", child_id)

        live = list(api_client.iter_children(trip_id, "hostings"))
        tombstones = list(
            api_client.iter_children(trip_id, "hostings", deleted=True)
        )
        check.equal(live, [], "gone from the live list")
        check.equal(tombstones, [{"id": child_id}], "but left a tombstone")

    ####################################################################
    #
    def test_fields_can_be_excluded_as_well_as_chosen(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a trip with a description
        WHEN:  it is fetched excluding that field
        THEN:  it is absent, sent as the API's own `fields!` spelling
        """
        created = api_client.create_trip(
            {"name": "Example", "description": "Wordy"}
        )
        assert isinstance(created, Created)

        trimmed = api_client.get_trip(
            created.payload["id"], exclude=["description"]
        )

        check.is_not_in("description", trimmed, "excluded")
        check.is_in("name", trimmed, "and the rest survived")

    ####################################################################
    #
    def test_a_login_that_returns_no_token_is_an_error(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: an auth route answering 200 with no token in the body
        WHEN:  the client logs in
        THEN:  it raises rather than carrying on unauthenticated and
               failing later with a confusing 401
        """
        store = paced_store(clock)
        store.throttle(count=1, status=200, body={})

        with client_for(store, clock) as client:
            with pytest.raises(APIError, match="without a token"):
                client.login("someone", "secret")

    ####################################################################
    #
    def test_a_page_linking_to_itself_is_caught(self, clock: FakeClock) -> None:
        """
        GIVEN: a paginated route whose next link points at the page it
               came from
        WHEN:  the pages are followed
        THEN:  it raises instead of paging forever, which unattended is
               the difference between a failed backup and a hung one
        """

        def loop(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "results": [{"id": 1}],
                    "next": f"{BASE}/v2/trips?page=1",
                    "previous": None,
                },
            )

        with TripsyClient(
            base_url=BASE,
            transport=httpx.MockTransport(loop),
        ) as client:
            with pytest.raises(APIError, match="pagination stalled"):
                list(client.iter_trips())


########################################################################
########################################################################
#
class TestIdempotentRetries:
    """
    Tests that a repeated write cannot create a second object.

    These are the ones that matter most.  A throttle is refused before
    anything happens, so repeating it is trivially safe; here the write
    lands and the *reply* is lost, which is the case a retry is dangerous
    in and the case `internal_identifier` exists for.
    """

    ####################################################################
    #
    @pytest.mark.parametrize(
        "method,key,threshold,expected",
        [
            # Anything idempotent by HTTP semantics, identifier or not.
            ("GET", None, 0, True),
            ("PATCH", None, 0, True),
            ("DELETE", None, 0, True),
            # A create with no identifier could duplicate, so it stands.
            ("POST", None, 0, False),
            ("POST", "", 0, False),
            # Child objects suppress a duplicate at any length.
            ("POST", "ab", 0, True),
            # Trips only above five characters, so a short one is not
            # retryable however present it is.
            ("POST", "ab", MIN_TRIP_IDENTIFIER_LENGTH, False),
            ("POST", "abcde", MIN_TRIP_IDENTIFIER_LENGTH, False),
            ("POST", "abcdef", MIN_TRIP_IDENTIFIER_LENGTH, True),
        ],
    )
    def test_retry_safety_follows_the_route_s_own_threshold(
        self,
        method: str,
        key: str | None,
        threshold: int,
        expected: bool,
    ) -> None:
        """
        GIVEN: a request and the identifier it carries
        WHEN:  the policy is asked whether repeating it is safe
        THEN:  a create is repeatable only when the route would actually
               suppress the duplicate, which for trips means an
               identifier longer than five characters
        """
        assert (
            DEFAULT_RETRIES.may_retry(
                method, idempotency_key=key, key_must_exceed=threshold
            )
            is expected
        )

    ####################################################################
    #
    def test_a_lost_reply_to_a_create_does_not_duplicate_the_trip(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a create whose write lands but whose reply is lost
        WHEN:  the client repeats it
        THEN:  the second attempt creates nothing, because the minted
               identifier suppresses it -- the property the whole
               re-runnable import rests on
        """
        store = paced_store(clock)
        store.lose_response(count=1, status=502)

        with client_for(store, clock) as client:
            client.create_trip(
                {"name": "Example", "internal_identifier": IDENT}
            )
            trips = client.list_trips_v1()

        posts = [m for m, _ in store.requests if m == "POST"]
        check.equal(len(posts), 2, "it really was re-sent")
        check.equal(len(trips), 1, "and only one trip exists")

    ####################################################################
    #
    def test_a_trip_identifier_too_short_to_suppress_is_refused(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: an identifier at or below the five-character threshold
        WHEN:  a trip is created with it
        THEN:  it is refused, because such a trip is created afresh by
               every re-run rather than recognised as already there
        """
        with pytest.raises(ValueError, match="too short"):
            api_client.create_trip(
                {"name": "Example", "internal_identifier": "abcde"}
            )

    ####################################################################
    #
    def test_a_child_identifier_may_be_short(self, clock: FakeClock) -> None:
        """
        GIVEN: a child create with a two-character identifier
        WHEN:  its reply is lost and the client repeats it
        THEN:  no duplicate appears, because child objects suppress at
               any length -- the threshold is the trips route's alone
        """
        store = paced_store(clock)
        trip = store.seed_trip(name="Example")
        store.lose_response(count=1, status=502)

        with client_for(store, clock) as client:
            client.create_child(
                trip["id"],
                "hostings",
                {"name": "Somewhere", "internal_identifier": "ab"},
            )
            children = list(client.iter_children(trip["id"], "hostings"))

        posts = [m for m, _ in store.requests if m == "POST"]
        check.equal(len(posts), 2, "re-sent")
        check.equal(len(children), 1, "and not duplicated")

    ####################################################################
    #
    def test_a_create_answering_201_with_no_object_is_an_error(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a create answered 201 but with an empty body
        WHEN:  the client reads it
        THEN:  it raises, rather than reporting AlreadyExists and
               telling the caller nothing was written when something was
        """
        store = paced_store(clock)
        store.throttle(count=1, status=201, body=None)

        with client_for(store, clock) as client:
            with pytest.raises(APIError, match="with no object"):
                client.create_trip(
                    {"name": "Example", "internal_identifier": IDENT}
                )

    ####################################################################
    #
    def test_update_trip_is_refused_on_a_create_too(
        self, api_client: TripsyClient
    ) -> None:
        """
        GIVEN: a create payload carrying update_trip
        WHEN:  it is sent
        THEN:  it is refused, matching the guard on update_child, since
               the trip is already named in the route
        """
        with pytest.raises(ValueError, match="no meaning on a create"):
            api_client.create_child(1, "hostings", {"update_trip": 2})

    ####################################################################
    #
    def test_a_slow_timeout_drives_the_pace_up(self, clock: FakeClock) -> None:
        """
        GIVEN: requests that hang and then time out
        WHEN:  the client gives up on them
        THEN:  the pace has risen to the ceiling, because a timeout
               storm is exactly when a slowdown is wanted and its full
               duration is what gets reported
        """

        def hang(request: httpx.Request) -> httpx.Response:
            clock.advance(30.0)
            raise httpx.ReadTimeout("timed out", request=request)

        pacer = Pacer(IMPORT)
        with TripsyClient(
            base_url=BASE,
            transport=httpx.MockTransport(hang),
            pacer=pacer,
            retries=RetryPolicy(jitter=0.0),
        ) as client:
            with pytest.raises(TransportError):
                client.list_trips_v1()

        check.equal(pacer.latency, pytest.approx(30.0), "the wait counted")
        check.equal(
            pacer.delay, pytest.approx(IMPORT.max_delay), "paced to the cap"
        )
        check.less_equal(
            pacer.delay, IMPORT.max_delay, "and no slower than the cap"
        )


########################################################################
########################################################################
#
class TestTimeouts:
    """
    Tests how a request that never answers feeds back into the pace.

    A timeout is three things at once and they are easy to conflate: a
    latency sample, a reason to back off, and a request that may have
    landed anyway.  Each is asserted here separately.
    """

    ####################################################################
    #
    @pytest.mark.parametrize(
        "kwargs,expected",
        [
            # How long to hang on for is the same judgement as how fast
            # to go, so it comes from the profile.
            ({"profile": IMPORT}, IMPORT.timeout),
            ({"profile": BACKUP}, BACKUP.timeout),
            ({"profile": INTERACTIVE}, INTERACTIVE.timeout),
            # Still overridable for a caller that knows better.
            ({"profile": IMPORT, "timeout": 3.0}, 3.0),
        ],
    )
    def test_the_timeout_comes_from_the_profile(
        self,
        clock: FakeClock,
        paced_tripsy: FakeTripsy,
        kwargs: dict[str, object],
        expected: float,
    ) -> None:
        """
        GIVEN: a client built for a particular kind of run
        WHEN:  its timeout is read
        THEN:  it is the profile's, unless one was named explicitly
        """
        with client_for(paced_tripsy, clock, **kwargs) as client:
            assert client.timeout == pytest.approx(expected)

    ####################################################################
    #
    @pytest.mark.parametrize(
        "payload,expected_sends",
        [
            # A timed-out write may well have landed.  Repeating it is
            # safe for the same reason repeating any create is: the
            # identifier makes the second attempt a no-op.
            ({"name": "Example", "internal_identifier": IDENT}, 4),
            # Without one, a timeout is exactly the case that would
            # produce a duplicate, so it is not repeated.
            ({"name": "Example"}, 1),
        ],
    )
    def test_a_timed_out_create_is_retried_only_when_idempotent(
        self,
        clock: FakeClock,
        payload: dict[str, str],
        expected_sends: int,
    ) -> None:
        """
        GIVEN: a create whose request hangs and times out
        WHEN:  the client decides whether to send it again
        THEN:  it repeats only when a repeat cannot create a second
               object -- a timeout is judged on the payload, exactly as
               a 502 is
        """
        sent = []

        def hang(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            clock.advance(30.0)
            raise httpx.ReadTimeout("timed out", request=request)

        with TripsyClient(
            base_url=BASE,
            transport=httpx.MockTransport(hang),
            retries=RetryPolicy(jitter=0.0),
        ) as client:
            with pytest.raises(TransportError):
                client.create_trip(payload)

        assert len(sent) == expected_sends

    ####################################################################
    #
    def test_a_timeout_is_counted_apart_from_a_throttle(
        self, clock: FakeClock
    ) -> None:
        """
        GIVEN: a run that both times out and is throttled
        WHEN:  its counters are read afterwards
        THEN:  the two are reported separately, so a run can be told
               apart from one the service merely asked to slow down
        """
        store = paced_store(clock)
        store.throttle(count=1, retry_after=1)

        with client_for(store, clock) as client:
            client.list_trips_v1()

            check.equal(client.pacer.throttles, 1, "the 429 counted")
            check.equal(client.pacer.failures, 0, "nothing failed outright")
