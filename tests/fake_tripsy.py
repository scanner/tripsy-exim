#!/usr/bin/env python
#
"""
An in-memory model of the Tripsy API, and an httpx transport onto it.

Everything through the exporter is developed against this rather than the
live service, so no test needs credentials or touches a real account.

The store holds the semantics and the transport is a thin router onto it.
That split matters because the API client does not exist yet: the store is
drivable with raw httpx today and by the client later, unchanged.

Time is explicit, and there are two clocks that must not be conflated.
`now` is the store's own clock, a plain attribute a test moves with
`advance`, so `updatedSince` behaviour is deterministic instead of
depending on how long the suite took to run.  `clock` is the separate
monotonic clock the pacer reads: when one is attached, `latency` seconds
are charged to it on every request, so a test can make the fake answer
slowly without the suite running slowly.

`throttle` queues canned responses -- a 429 with a `Retry-After`, say --
which the transport serves before routing.  Unlike everything else here,
this models no observed behaviour and is not a claim about what Tripsy
does.  Probing found no rate-limit headers of any kind and no documented
limit, so what the service sends when it has had enough is simply not
known.  The client is built to read a directive and pace itself by one
anyway, and these canned responses are how that is exercised.  If Tripsy
turns out to send something else, or nothing, the client is no worse off
-- it already paces on response latency alone.

Five of the behaviours modelled here were verified against the live API on
2026-09-09.  Hiding `price` and `currency` without expense permission was
not, and cannot be: the account is premium, so that response can never be
observed from outside.  It is modelled from the documentation, and this
fake is the only place that path is ever exercised.
"""

# system imports
import json
import re
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

# 3rd party imports
import httpx

# Project imports
from tests.clock import FakeClock

# Child collections, by the path segment the API uses for the list route.
# The singular form is the detail route.
#
COLLECTIONS: dict[str, str] = {
    "hostings": "hosting",
    "activities": "activity",
    "transportations": "transportation",
    "expenses": "expense",
    "collaborators": "collaborator",
}

# v2 pages at 100.  A real exported trip carries 121 events, so this is
# reached on the first import rather than hypothetically.
#
PAGE_SIZE = 100

# The server subtracts this before filtering on updatedSince, which
# absorbs clock skew.  Verified 2026-09-09.
#
UPDATED_SINCE_CUSHION = timedelta(days=2)

# Trip-level duplicate suppression only engages above this length.
#
MIN_IDENTIFIER_LENGTH = 5

# Withheld together when the caller cannot see expenses.
#
EXPENSE_FIELDS = ("price", "currency")

BASE = "https://api.tripsy.app"


########################################################################
########################################################################
#
class FakeTripsy:
    """The documented Tripsy API, in memory."""

    ####################################################################
    #
    def __init__(
        self,
        *,
        can_see_expenses: bool = True,
        page_size: int = PAGE_SIZE,
        now: datetime | None = None,
        clock: FakeClock | None = None,
        latency: float = 0.0,
    ) -> None:
        """
        Args:
            can_see_expenses: When False, `price` and `currency` are
                withheld from every response, as they are for a caller
                without expense permission.
            page_size: Results per page on the paginated endpoints.
            now: The store's clock.  Defaults to a fixed instant so runs
                are reproducible.
            clock: The monotonic clock `latency` is charged to.  Attach
                the one the pacer reads to make the fake appear slow.
            latency: Seconds each request takes to answer.
        """
        self.can_see_expenses = can_see_expenses
        self.page_size = page_size
        self.now = now or datetime(2027, 1, 1, tzinfo=UTC)
        self.clock = clock
        self.latency = latency

        # Canned responses served ahead of the real routing, oldest
        # first, so a test reads as "the third call gets a 429".
        #
        self._canned: deque[tuple[int, Any, dict[str, str]]] = deque()

        # Statuses replacing the response *after* the request has been
        # carried out, oldest first.  See `lose_response`.
        #
        self._losses: deque[int] = deque()

        self.owner_id = 1
        self._next_id = 1
        self._trips: dict[int, dict[str, Any]] = {}
        self._children: dict[tuple[int, str], dict[int, dict[str, Any]]] = {}
        self._deleted_trips: set[int] = set()
        self._deleted_children: set[tuple[int, str, int]] = set()

        # Requests served, for tests that care about call volume -- a full
        # import is over a thousand writes.
        #
        self.requests: list[tuple[str, str]] = []

    ####################################################################
    #
    def advance(self, **delta: float) -> None:
        """Move the clock, e.g. `advance(days=3)`."""
        self.now = self.now + timedelta(**delta)

    ####################################################################
    #
    def throttle(
        self,
        *,
        count: int = 1,
        status: int = 429,
        retry_after: float | str | None = None,
        body: Any = None,
    ) -> None:
        """
        Queue canned throttle responses ahead of the next real ones.

        Speculative by construction: no throttle has been observed from
        Tripsy, so the shapes here are the conventional ones rather than
        measured ones.  A test using this asserts what the client does
        when told to slow down, never what the service does.

        Args:
            count: How many consecutive requests are answered this way.
            status: The status to answer with.
            retry_after: Sent as a `Retry-After` header when given.  A
                string is passed through untouched, so a test can send
                an HTTP-date or a malformed value.
            body: The response body, or None for an empty one.
        """
        headers: dict[str, str] = {}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        for _ in range(count):
            self._canned.append((status, body, dict(headers)))

    ####################################################################
    #
    def lose_response(self, *, count: int = 1, status: int = 502) -> None:
        """
        Carry a request out and then lose the answer to it.

        Distinct from `throttle`, and the distinction is the whole point.
        A throttle is refused before anything happens, so repeating it is
        trivially safe.  This one writes, and *then* fails -- the caller
        cannot tell which, and repeating it is safe only because a
        duplicate `internal_identifier` creates nothing.  Without this,
        a suite can assert that retries happen but never that they are
        harmless.

        Args:
            count: How many consecutive responses are lost.
            status: The status the caller sees instead of the real one.
        """
        for _ in range(count):
            self._losses.append(status)

    ####################################################################
    #
    def _allocate(self) -> int:
        """Hand out the next object id."""
        value = self._next_id
        self._next_id += 1
        return value

    ####################################################################
    #
    def _stamp(self) -> str:
        """The clock, in the format the API emits."""
        return self.now.strftime("%Y-%m-%dT%H:%M:%SZ")

    ####################################################################
    #
    def seed_trip(self, **fields: Any) -> dict[str, Any]:
        """Put a trip in the store directly, bypassing create semantics."""
        trip = self._store_trip(fields)
        return dict(trip)

    ####################################################################
    #
    def seed_child(
        self, trip_id: int, collection: str, **fields: Any
    ) -> dict[str, Any]:
        """Put a child object in the store directly."""
        return dict(self._store_child(trip_id, collection, fields))

    ####################################################################
    #
    def _store_trip(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write a trip, assigning an id and the hidden update stamp."""
        trip_id = self._allocate()
        trip = {
            "id": trip_id,
            "owner": self.owner_id,
            "collaborators": 1,
            "collaborators_count": 1,
            **payload,
        }

        # Trips expose no created_at or updated_at in either API version,
        # but the server plainly has one -- updatedSince filters on it.
        # Keeping it out of band is what makes the watermark wall-clock
        # time rather than something read off the data.
        #
        trip["_updated_at"] = self.now
        self._trips[trip_id] = trip
        return trip

    ####################################################################
    #
    def _store_child(
        self, trip_id: int, collection: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Write a child object under a trip."""
        child_id = self._allocate()
        child = {
            "id": child_id,
            "trip": trip_id,
            "created_at": self._stamp(),
            "updated_at": self._stamp(),
            **payload,
        }
        if collection in ("hostings", "activities", "transportations"):
            child.setdefault(
                "owner",
                {
                    "id": self.owner_id,
                    "name": "Fake Owner",
                    "email": "owner@example.invalid",
                    "photo_url": None,
                },
            )
        child["_updated_at"] = self.now
        self._children.setdefault((trip_id, collection), {})[child_id] = child

        # A nested write bumps the parent, which is why updatedSince on
        # trips catches changes made through children.
        #
        if trip_id in self._trips:
            self._trips[trip_id]["_updated_at"] = self.now
        return child

    ####################################################################
    #
    def _find_trip_by_identifier(
        self, identifier: str
    ) -> dict[str, Any] | None:
        """Locate a live trip by its internal_identifier."""
        for trip in self._trips.values():
            if trip.get("internal_identifier") == identifier:
                return trip
        return None

    ####################################################################
    #
    def create_trip(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """
        POST /v1/trips.

        Returns:
            (201, trip) normally, or (200, None) when the identifier
            already belongs to a trip of the same owner and is long
            enough for suppression to engage.
        """
        identifier = payload.get("internal_identifier") or ""
        if (
            len(identifier) > MIN_IDENTIFIER_LENGTH
            and self._find_trip_by_identifier(identifier) is not None
        ):
            return 200, None
        return 201, self._public(self._store_trip(payload))

    ####################################################################
    #
    def create_child(
        self, trip_id: int, collection: str, payload: dict[str, Any]
    ) -> tuple[int, Any]:
        """
        POST /v1/trip/{id}/{collection}.

        A duplicate identifier within the same trip returns an empty 200,
        with no length threshold -- that applies to trips only.
        """
        if trip_id not in self._trips:
            return 404, {"detail": "Not found."}

        identifier = payload.get("internal_identifier")
        if identifier:
            existing = self._children.get((trip_id, collection), {})
            for child in existing.values():
                if child.get("internal_identifier") == identifier:
                    return 200, None
        return 201, self._public(
            self._store_child(trip_id, collection, payload)
        )

    ####################################################################
    #
    def update_child(
        self,
        trip_id: int,
        collection: str,
        child_id: int,
        payload: dict[str, Any],
    ) -> tuple[int, Any]:
        """
        PUT|PATCH on a child object.

        `update_trip` in the body *moves* the object to another trip and
        answers an empty 200.  Nothing above the transport can catch a
        client that sends it by accident, so the fake reproduces it.
        """
        children = self._children.get((trip_id, collection), {})
        if child_id not in children:
            return 404, {"detail": "Not found."}

        child = children[child_id]
        destination = payload.get("update_trip")
        if destination is not None:
            if destination not in self._trips:
                return 400, {"update_trip": ["No such trip."]}
            del children[child_id]
            child["trip"] = destination
            child["_updated_at"] = self.now
            self._children.setdefault((destination, collection), {})[
                child_id
            ] = child
            return 200, None

        child.update(payload)
        child["updated_at"] = self._stamp()
        child["_updated_at"] = self.now
        return 200, self._public(child)

    ####################################################################
    #
    def delete_trip(self, trip_id: int) -> tuple[int, Any]:
        """DELETE /v1/trips/{id}.  Soft, so the tombstone survives."""
        if trip_id not in self._trips:
            return 404, {"detail": "Not found."}
        self._deleted_trips.add(trip_id)
        self._trips[trip_id]["_updated_at"] = self.now
        return 204, None

    ####################################################################
    #
    def delete_child(
        self, trip_id: int, collection: str, child_id: int
    ) -> tuple[int, Any]:
        """DELETE on a child object.  Also soft."""
        if child_id not in self._children.get((trip_id, collection), {}):
            return 404, {"detail": "Not found."}
        self._deleted_children.add((trip_id, collection, child_id))
        return 204, None

    ####################################################################
    #
    def _public(
        self,
        obj: dict[str, Any],
        fields: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Project a stored object into what a caller is allowed to see.

        Args:
            obj: The stored object, including out-of-band keys.
            fields: When given, the only fields returned.
            exclude: Fields to drop.

        Returns:
            A response-shaped dict.
        """
        result = {k: v for k, v in obj.items() if not k.startswith("_")}

        if not self.can_see_expenses:
            for name in EXPENSE_FIELDS:
                result.pop(name, None)
        if fields:
            result = {k: v for k, v in result.items() if k in fields}
        if exclude:
            result = {k: v for k, v in result.items() if k not in exclude}
        return result

    ####################################################################
    #
    def _filter_updated_since(
        self, objects: list[dict[str, Any]], raw: str | None
    ) -> list[dict[str, Any]]:
        """Apply updatedSince, minus the server's two-day cushion."""
        if not raw:
            return objects
        try:
            since = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return objects
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        cutoff = since - UPDATED_SINCE_CUSHION
        return [o for o in objects if o["_updated_at"] >= cutoff]

    ####################################################################
    #
    def _paginate(
        self,
        results: list[Any],
        path: str,
        page: int,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Wrap results in the standard paginated envelope.

        The page links carry the rest of the query forward, as DRF's
        pagination does by building them from the whole request URL.  A
        fake that emitted a bare `?page=` would let a client that follows
        the link silently widen an `updatedSince` or `fields` filter on
        page two, and the suite would not notice.
        """
        start = (page - 1) * self.page_size
        window = results[start : start + self.page_size]
        has_next = start + self.page_size < len(results)

        def link(number: int) -> str:
            query = {k: v for k, v in (params or {}).items() if k != "page"}
            query["page"] = str(number)
            return f"{BASE}{path}?{urlencode(query)}"

        return {
            "count": len(results),
            "next": link(page + 1) if has_next else None,
            "previous": link(page - 1) if page > 1 else None,
            "results": window,
        }

    ####################################################################
    #
    def list_trips_v1(self, params: dict[str, str]) -> tuple[int, Any]:
        """
        GET /v1/trips.

        This route is the envelope exception: a bare `results` key with no
        count, next or previous.  A fake that returned the paginated shape
        everywhere would hide a client that only handles one.
        """
        live = [
            t
            for t in self._trips.values()
            if t["id"] not in self._deleted_trips
        ]
        live = self._filter_updated_since(live, params.get("updatedSince"))
        fields, exclude = _field_selection(params)
        return 200, {
            "results": [self._public(t, fields, exclude) for t in live]
        }

    ####################################################################
    #
    def list_trips_v2(self, params: dict[str, str]) -> tuple[int, Any]:
        """GET /v2/trips -- paginated, and tombstones when deleted=true."""
        if params.get("deleted") == "true":
            tombstones = [{"id": i} for i in sorted(self._deleted_trips)]
            return 200, self._paginate(
                tombstones, "/v2/trips", int(params.get("page", 1)), params
            )

        live = [
            t
            for t in self._trips.values()
            if t["id"] not in self._deleted_trips
        ]
        live = self._filter_updated_since(live, params.get("updatedSince"))
        fields, exclude = _field_selection(params)

        # v2 reports the collaborator count as `collaborators` and does not
        # send `collaborators_count` or `emails` at all.
        #
        results = [
            self._public(
                t, fields, (exclude or []) + ["collaborators_count", "emails"]
            )
            for t in live
        ]
        return 200, self._paginate(
            results, "/v2/trips", int(params.get("page", 1)), params
        )

    ####################################################################
    #
    def get_trip(self, trip_id: int, params: dict[str, str]) -> tuple[int, Any]:
        """GET /v1/trips/{id}."""
        if trip_id not in self._trips or trip_id in self._deleted_trips:
            return 404, {"detail": "Not found."}
        fields, exclude = _field_selection(params)
        return 200, self._public(self._trips[trip_id], fields, exclude)

    ####################################################################
    #
    def update_trip(
        self, trip_id: int, payload: dict[str, Any]
    ) -> tuple[int, Any]:
        """PUT|PATCH /v1/trips/{id}."""
        if trip_id not in self._trips or trip_id in self._deleted_trips:
            return 404, {"detail": "Not found."}
        trip = self._trips[trip_id]
        trip.update(payload)
        trip["_updated_at"] = self.now
        return 200, self._public(trip)

    ####################################################################
    #
    def list_children(
        self,
        trip_id: int,
        collection: str,
        params: dict[str, str],
        version: str,
    ) -> tuple[int, Any]:
        """GET the child list route, in either API version."""
        if trip_id not in self._trips:
            return 404, {"detail": "Not found."}

        stored = self._children.get((trip_id, collection), {})
        if params.get("deleted") == "true":
            tombstones = [
                {"id": cid}
                for cid in sorted(stored)
                if (trip_id, collection, cid) in self._deleted_children
            ]
            return 200, self._paginate(
                tombstones,
                f"/{version}/trip/{trip_id}/{collection}",
                int(params.get("page", 1)),
                params,
            )

        live = [
            child
            for cid, child in sorted(stored.items())
            if (trip_id, collection, cid) not in self._deleted_children
        ]
        live = self._filter_updated_since(live, params.get("updatedSince"))

        for key, field in (
            ("activityType", "activity_type"),
            ("transportationType", "transportation_type"),
        ):
            if key in params:
                live = [c for c in live if c.get(field) == params[key]]

        fields, exclude = _field_selection(params)
        if version == "v2":
            # emails are never embedded in a v2 payload.
            exclude = (exclude or []) + ["emails"]
        results = [self._public(c, fields, exclude) for c in live]
        return 200, self._paginate(
            results,
            f"/{version}/trip/{trip_id}/{collection}",
            int(params.get("page", 1)),
            params,
        )

    ####################################################################
    #
    def get_child(
        self,
        trip_id: int,
        collection: str,
        child_id: int,
        params: dict[str, str],
    ) -> tuple[int, Any]:
        """GET one child object."""
        stored = self._children.get((trip_id, collection), {})
        if (
            child_id not in stored
            or (trip_id, collection, child_id) in self._deleted_children
        ):
            return 404, {"detail": "Not found."}
        fields, exclude = _field_selection(params)
        return 200, self._public(stored[child_id], fields, exclude)


####################################################################
#
def _field_selection(
    params: dict[str, str],
) -> tuple[list[str] | None, list[str] | None]:
    """Read the `fields=` and `fields!=` query parameters."""
    include = params.get("fields")
    exclude = params.get("fields!")
    return (
        include.split(",") if include else None,
        exclude.split(",") if exclude else None,
    )


# /v1/trip/42/hostings, /v2/trip/42/hosting/101, and so on.
#
_CHILD_LIST = re.compile(r"^/(v1|v2)/trip/(\d+)/([a-z]+)$")
_CHILD_DETAIL = re.compile(r"^/(v1|v2)/trip/(\d+)/([a-z]+)/(\d+)$")
_TRIP_DETAIL = re.compile(r"^/v1/trips/(\d+)$")

# Detail routes use the singular collection name.
#
_SINGULAR_TO_PLURAL = {v: k for k, v in COLLECTIONS.items()}


####################################################################
#
def transport(store: FakeTripsy) -> httpx.MockTransport:
    """
    Build an httpx transport that answers from the store.

    Mount it on a client and every request is served in memory:

        client = httpx.Client(base_url=BASE, transport=transport(store))

    Args:
        store: The FakeTripsy holding the data and the semantics.

    Returns:
        A transport suitable for `httpx.Client(transport=...)`.
    """

    ####################################################################
    #
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        params = dict(request.url.params)
        store.requests.append((method, path))

        # Charged before answering, so the caller measures the latency on
        # the request that incurred it rather than the one after.
        #
        if store.clock is not None and store.latency:
            store.clock.advance(store.latency)

        # A queued throttle answers whatever the request was, which is
        # what makes it a throttle rather than a routing rule.
        #
        if store._canned:
            return _respond(*store._canned.popleft())

        body: dict[str, Any] = {}
        if request.content:
            body = json.loads(request.content)

        # Losing the answer happens after the store has acted, so the
        # write is real and only the reply is gone.
        #
        if store._losses:
            status = store._losses.popleft()
            _route(store, path, method, params, body)
            return _respond(status, {"detail": "Lost."})

        return _route(store, path, method, params, body)

    return httpx.MockTransport(handler)


####################################################################
#
def _route(
    store: FakeTripsy,
    path: str,
    method: str,
    params: dict[str, str],
    body: dict[str, Any],
) -> httpx.Response:
    """
    Dispatch one request to the store method that answers it.

    Split out from the handler so a request can be carried out and its
    answer thrown away afterwards -- see `lose_response`.

    Args:
        store: The store holding the data and the semantics.
        path: The URL path, already extracted.
        method: The HTTP method, upper case.
        params: Query parameters, flattened to strings.  Repeated keys
            keep only the last value, which no route here depends on.
        body: The decoded JSON body, or an empty dict when there was
            none -- so a route may read it without checking first.

    Returns:
        The response, including a 404 for any path this fake does not
        model.
    """
    if path == "/auth" and method == "POST":
        return _respond(200, {"token": "fake-token-for-tests"})

    if path == "/v1/trips":
        if method == "POST":
            return _respond(*store.create_trip(body))
        return _respond(*store.list_trips_v1(params))

    if path == "/v2/trips" and method == "GET":
        return _respond(*store.list_trips_v2(params))

    match = _TRIP_DETAIL.match(path)
    if match:
        trip_id = int(match.group(1))
        if method == "DELETE":
            return _respond(*store.delete_trip(trip_id))
        if method in ("PUT", "PATCH"):
            return _respond(*store.update_trip(trip_id, body))
        return _respond(*store.get_trip(trip_id, params))

    match = _CHILD_LIST.match(path)
    if match and match.group(3) in COLLECTIONS:
        version, trip_id, collection = (
            match.group(1),
            int(match.group(2)),
            match.group(3),
        )
        if method == "POST":
            return _respond(*store.create_child(trip_id, collection, body))
        return _respond(
            *store.list_children(trip_id, collection, params, version)
        )

    match = _CHILD_DETAIL.match(path)
    if match and match.group(3) in _SINGULAR_TO_PLURAL:
        trip_id = int(match.group(2))
        collection = _SINGULAR_TO_PLURAL[match.group(3)]
        child_id = int(match.group(4))
        if method == "DELETE":
            return _respond(*store.delete_child(trip_id, collection, child_id))
        if method in ("PUT", "PATCH"):
            return _respond(
                *store.update_child(trip_id, collection, child_id, body)
            )
        return _respond(*store.get_child(trip_id, collection, child_id, params))

    return _respond(404, {"detail": "No such route in the fake."})


####################################################################
#
def _respond(
    status: int, body: Any, headers: dict[str, str] | None = None
) -> httpx.Response:
    """Build a response, with an empty body when there is none to send."""
    if body is None:
        return httpx.Response(status, headers=headers)
    return httpx.Response(status, json=body, headers=headers)
