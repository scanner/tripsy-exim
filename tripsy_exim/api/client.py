#!/usr/bin/env python
#
"""
The typed client for the Tripsy public API.

Owns routes, payload shapes, pagination, retries, and the mapping from
statuses to exceptions.  It knows nothing about import or export policy and
never decides what should be written -- callers hand it a token and tell it
what to do.

Two version rules run through every route.  Writes go to v1, which is the
only version that accepts them; reads go to v2, which paginates at 100 and
is the only version that reports deletions.  The exception is
`GET /v1/trips`, which answers a bare `results` list with no envelope and
is the cheapest way to map identifiers to ids.

Every call is paced.  The pacer lives under an `httpx.Client` as a
transport, so retried sends and any route added later are paced too,
without each one having to remember.  Pass one `Pacer` to several clients
and the pace is shared across all of them, which is what "a session" means
here.
"""

# system imports
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

# 3rd party imports
import httpx

# Project imports
from tripsy_exim.api.auth import TokenAuth, TripsyAuth
from tripsy_exim.api.errors import (
    APIError,
    TransportError,
    error_for,
)
from tripsy_exim.api.pacing import (
    IMPORT,
    Pacer,
    PacingProfile,
    directive_seconds,
)
from tripsy_exim.api.retry import DEFAULT as DEFAULT_RETRIES
from tripsy_exim.api.retry import RetryPolicy
from tripsy_exim.api.transport import PacedTransport

BASE = "https://api.tripsy.app"

# List routes use the plural, detail routes the singular.
#
COLLECTIONS: dict[str, str] = {
    "hostings": "hosting",
    "activities": "activity",
    "transportations": "transportation",
    "expenses": "expense",
    "collaborators": "collaborator",
}

# Trip-level duplicate suppression only engages above this length --
# verified against the live API on 2026-09-09.  Child objects suppress at
# any length.  Below it a create is not idempotent, so it is neither
# retried nor accepted.  `mint()` never produces anything this short.
#
MIN_TRIP_IDENTIFIER_LENGTH = 5

# Moving an object to another trip is what this field does on a child
# PUT or PATCH.  It is never a value to be carried along from a fetched
# payload, so it is refused unless `move_child` put it there on purpose.
#
MOVE_FIELD = "update_trip"


########################################################################
########################################################################
#
@dataclass(frozen=True)
class Created:
    """The object was created.  `payload` is what Tripsy answered with."""

    payload: dict[str, Any]


########################################################################
########################################################################
#
@dataclass(frozen=True)
class AlreadyExists:
    """
    An object with this `internal_identifier` was already there.

    Tripsy answers a duplicate create with an empty 200 rather than an
    error, and the response carries no id -- so this says only that
    nothing was created.  Resolving the identifier to an id, when the
    caller needs one, is `trip_ids_by_identifier`.
    """

    internal_identifier: str | None


WriteResult = Created | AlreadyExists


########################################################################
########################################################################
#
class TripsyClient:
    """Routes, pagination, and error mapping over a paced transport."""

    ####################################################################
    #
    def __init__(
        self,
        *,
        token: str | None = None,
        auth: TripsyAuth | None = None,
        base_url: str = BASE,
        profile: PacingProfile = IMPORT,
        pacer: Pacer | None = None,
        retries: RetryPolicy = DEFAULT_RETRIES,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """
        Args:
            token: A token from `POST /auth`, if one is already held.
            auth: A prepared auth object, for OAuth2 or a shared token.
                Takes precedence over `token`.
            base_url: The API root.
            profile: The pace to keep, when building a pacer.  Ignored if
                `pacer` is given.
            pacer: An existing pacer to share, which is how two clients
                pace as one session.
            retries: How persistently failures are repeated.
            timeout: Seconds before a request is abandoned.  None takes
                the profile's, which is the usual case -- how long to
                hang on for is the same judgement as how fast to go.
            transport: The transport underneath the pacing one.  Tests
                pass the fake's; production leaves it alone.
        """
        self.auth = auth if auth is not None else TokenAuth(token or "")
        self.retries = retries
        self.pacer = pacer if pacer is not None else Pacer(profile)

        # Read off the pacer rather than the `profile` argument, since a
        # shared pacer overrides it and the timeout must match the
        # profile actually in force.
        #
        self.timeout = (
            timeout if timeout is not None else self.pacer.profile.timeout
        )
        self._http = httpx.Client(
            base_url=base_url,
            auth=self.auth,
            timeout=self.timeout,
            transport=PacedTransport(self.pacer, transport),
        )

    ####################################################################
    #
    def __enter__(self) -> TripsyClient:
        return self

    ####################################################################
    #
    def __exit__(self, *exc_info: object) -> None:
        self.close()

    ####################################################################
    #
    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    ####################################################################
    #
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        key_must_exceed: int = 0,
    ) -> httpx.Response:
        """
        Send one request, repeating it while that is safe and useful.

        Retrying a POST is allowed only when the payload carries an
        `internal_identifier` long enough for the route to suppress a
        duplicate, because that is what makes a create a no-op the second
        time.  A server directive longer than the profile permits ends
        the attempts immediately: waiting it out would park the run for
        an unbounded time on a header that cannot be sanity-checked, so
        it is raised instead.

        A timeout arrives here as an `httpx.TransportError` and is
        retried on the same terms as any other failure -- which matters,
        because a timed-out write may well have landed.  That is safe for
        exactly the reason a create is retryable at all: the identifier
        makes the second attempt a no-op.  A create without one is not
        repeated after a timeout any more than after a 502.

        Args:
            method: The HTTP method.
            path: Path below the base URL.
            params: Query parameters, already flattened to strings by
                `query()`.  Values are sent as given; None entries must
                be dropped before they reach here or they arrive at the
                API as the literal string 'None'.
            json: The request body, if any.  When it is a dict its
                `internal_identifier` decides whether a failed attempt
                may be repeated, so a create's payload is load-bearing
                here and not merely passed through.
            key_must_exceed: Length the identifier must exceed for this
                route to treat a repeat as a duplicate.  0 means any
                non-empty identifier will do, which is true of every
                route except trips.

        Returns:
            The successful response.

        Raises:
            APIError: The request failed and will not be repeated.
            TransportError: No response was obtained, after any retries.
        """
        # Whether a repeat is safe is a property of the body, not of the
        # route, so it is read off the payload once and carried through
        # every attempt.
        #
        key = None
        if isinstance(json, dict):
            key = json.get("internal_identifier")

        attempt = 1
        while True:
            # Recomputed each time round, because the attempt budget is
            # half of what makes a request retryable and it shrinks.
            #
            retryable = (
                attempt < self.retries.max_attempts
                and self.retries.may_retry(
                    method,
                    idempotency_key=key,
                    key_must_exceed=key_must_exceed,
                )
            )

            # The send itself.  Waiting out the pace and reporting the
            # result back to it both happen below this call, inside the
            # transport, so nothing here has to remember to do either.
            #
            try:
                response = self._http.request(
                    method, path, params=dict(params or {}), json=json
                )
            except httpx.TransportError as exc:
                # No response at all: a timeout, a refused connection, a
                # dropped socket.  Indistinguishable from each other here
                # and treated alike.
                #
                if not retryable:
                    raise TransportError(
                        f"{method} {path} failed: {exc!r}"
                    ) from exc
                self._hold_off(attempt)
                attempt += 1
                continue

            if response.status_code < 400:
                return response

            # A refusal may name its own wait.  Honouring one longer than
            # the profile allows would park an unattended run for as long
            # as the header said, so past that point it is raised instead
            # of slept -- the pacer has already capped what it took from
            # the same header for its floor.
            #
            directive = directive_seconds(response.headers)
            too_long = (
                directive is not None
                and directive > self.pacer.profile.max_retry_after
            )
            if (
                retryable
                and not too_long
                and response.status_code in self.retries.statuses
            ):
                self._hold_off(attempt)
                attempt += 1
                continue

            raise error_for(response, retry_after=directive)

    ####################################################################
    #
    def _hold_off(self, attempt: int) -> None:
        """Push the next send out by this attempt's backoff."""
        self.pacer.defer(self.retries.backoff(attempt + 1))

    ####################################################################
    #
    def _json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        """Send a request and decode the body, or None if it was empty."""
        response = self.request(method, path, params=params, json=json)
        if not response.content:
            return None
        return response.json()

    # ==================================================================
    # Authentication
    # ==================================================================

    ####################################################################
    #
    def login(self, username: str, password: str) -> str:
        """
        Trade credentials for a token and start using it.

        Args:
            username: The account's username.
            password: The account's password.

        Returns:
            The token, which is also now set on this client's auth.

        Raises:
            APIError: The credentials were rejected.
        """
        body = self._json(
            "POST",
            "/auth",
            json={"username": username, "password": password},
        )
        token = (body or {}).get("token")
        if not token:
            raise APIError(
                "POST /auth answered without a token",
                status_code=200,
                method="POST",
                url="/auth",
                payload=body,
            )
        self.auth.token = str(token)
        return self.auth.token

    # ==================================================================
    # Trips
    # ==================================================================

    ####################################################################
    #
    def create_trip(self, payload: Mapping[str, Any]) -> WriteResult:
        """
        POST /v1/trips.

        Args:
            payload: The trip body, normally a model's
                `writable_payload()`.

        Returns:
            `Created` with the new trip, or `AlreadyExists` when the
            identifier was already taken and nothing was written.

        Raises:
            ValueError: The identifier is too short for Tripsy to
                suppress a duplicate of it.  Such a trip would be
                created afresh by every re-run of an import, which is
                the one thing `internal_identifier` is here to prevent,
                so it is refused rather than written.
        """
        identifier = payload.get("internal_identifier")
        if (
            identifier is not None
            and len(str(identifier)) <= MIN_TRIP_IDENTIFIER_LENGTH
        ):
            raise ValueError(
                f"internal_identifier {identifier!r} is too short: trips "
                f"suppress duplicates only above "
                f"{MIN_TRIP_IDENTIFIER_LENGTH} characters, so this trip "
                f"would be created again on every run"
            )
        return self._write(
            "/v1/trips",
            payload,
            key_must_exceed=MIN_TRIP_IDENTIFIER_LENGTH,
        )

    ####################################################################
    #
    def get_trip(
        self,
        trip_id: int,
        *,
        fields: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> dict[str, Any]:
        """GET /v1/trips/{id}, optionally a subset of its fields."""
        body = self._json(
            "GET",
            f"/v1/trips/{trip_id}",
            params=query(fields=fields, exclude=exclude),
        )
        return dict(body or {})

    ####################################################################
    #
    def update_trip(
        self, trip_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """PATCH /v1/trips/{id} with only the fields given."""
        body = self._json("PATCH", f"/v1/trips/{trip_id}", json=dict(payload))
        return dict(body or {})

    ####################################################################
    #
    def delete_trip(self, trip_id: int) -> None:
        """DELETE /v1/trips/{id}.  Soft: a tombstone remains."""
        self.request("DELETE", f"/v1/trips/{trip_id}")

    ####################################################################
    #
    def list_trips_v1(
        self,
        *,
        updated_since: str | None = None,
        fields: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        GET /v1/trips -- the one route with no pagination envelope.

        It answers a bare `results` list, so every trip arrives in one
        response and there is nothing to page through.

        Returns:
            Every matching trip.
        """
        body = self._json(
            "GET",
            "/v1/trips",
            params=query(
                fields=fields, exclude=exclude, updatedSince=updated_since
            ),
        )
        results = (body or {}).get("results", [])
        return [dict(t) for t in results]

    ####################################################################
    #
    def trip_ids_by_identifier(self) -> dict[str, int]:
        """
        Map every `internal_identifier` in the account to its trip id.

        This is how a create that answered `AlreadyExists` is resolved to
        an id.  One lean request covers the whole account, so nothing has
        to be cached for correctness.

        Returns:
            Identifiers to ids, skipping trips that carry no identifier.
        """
        trips = self.list_trips_v1(fields=["id", "internal_identifier"])
        return {
            trip["internal_identifier"]: trip["id"]
            for trip in trips
            if trip.get("internal_identifier") and trip.get("id") is not None
        }

    ####################################################################
    #
    def iter_trips(
        self,
        *,
        updated_since: str | None = None,
        deleted: bool = False,
        fields: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """
        GET /v2/trips, following the pages.

        Args:
            updated_since: Return only trips touched since this instant,
                directly or through a nested object.  The server
                subtracts two days before filtering.
            deleted: Return tombstones instead of live trips.  Each is
                `{"id": N}` and nothing else.
            fields: Only these fields.
            exclude: Everything but these fields.

        Yields:
            One trip at a time, across every page.
        """
        yield from self.paginate(
            "/v2/trips",
            query(
                fields=fields,
                exclude=exclude,
                updatedSince=updated_since,
                deleted="true" if deleted else None,
            ),
        )

    # ==================================================================
    # Child objects
    # ==================================================================

    ####################################################################
    #
    def create_child(
        self,
        trip_id: int,
        collection: str,
        payload: Mapping[str, Any],
    ) -> WriteResult:
        """
        POST /v1/trip/{id}/{collection}.

        Args:
            trip_id: The trip to hang the object off.
            collection: One of the plural collection names.
            payload: The object body.

        Returns:
            `Created` with the new object, or `AlreadyExists` when that
            identifier is already used within this trip.

        Raises:
            ValueError: The payload carries `update_trip`, which belongs
                to no create and means something quite different on the
                update that follows.
        """
        if MOVE_FIELD in payload:
            raise ValueError(
                f"{MOVE_FIELD!r} has no meaning on a create -- the trip is "
                f"already named in the route"
            )
        path = f"/v1/trip/{trip_id}/{_collection(collection)}"
        return self._write(path, payload)

    ####################################################################
    #
    def get_child(
        self,
        trip_id: int,
        collection: str,
        child_id: int,
        *,
        fields: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> dict[str, Any]:
        """GET one child object."""
        body = self._json(
            "GET",
            self._detail(trip_id, collection, child_id),
            params=query(fields=fields, exclude=exclude),
        )
        return dict(body or {})

    ####################################################################
    #
    def update_child(
        self,
        trip_id: int,
        collection: str,
        child_id: int,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """
        PATCH one child object.

        Raises:
            ValueError: The payload carries `update_trip`, which would
                move the object to another trip and answer an empty 200
                rather than updating anything.  Nothing above the
                transport could tell that apart from a successful update,
                so it is refused here.  `move_child` is the way to do it
                on purpose.
        """
        if MOVE_FIELD in payload:
            raise ValueError(
                f"{MOVE_FIELD!r} in an update payload moves the object to "
                f"another trip -- use move_child() if that is intended"
            )
        body = self._json(
            "PATCH",
            self._detail(trip_id, collection, child_id),
            json=dict(payload),
        )
        return dict(body or {})

    ####################################################################
    #
    def move_child(
        self,
        trip_id: int,
        collection: str,
        child_id: int,
        destination_trip_id: int,
    ) -> None:
        """
        Move a child object to another trip.

        The API answers this with an empty 200 and no object, so there is
        nothing to return and nothing to check beyond the status.
        """
        self.request(
            "PATCH",
            self._detail(trip_id, collection, child_id),
            json={MOVE_FIELD: destination_trip_id},
        )

    ####################################################################
    #
    def delete_child(
        self, trip_id: int, collection: str, child_id: int
    ) -> None:
        """DELETE one child object.  Soft, like a trip."""
        self.request("DELETE", self._detail(trip_id, collection, child_id))

    ####################################################################
    #
    def iter_children(
        self,
        trip_id: int,
        collection: str,
        *,
        updated_since: str | None = None,
        deleted: bool = False,
        fields: list[str] | None = None,
        exclude: list[str] | None = None,
        **filters: str | None,
    ) -> Iterator[dict[str, Any]]:
        """
        GET a trip's child collection from v2, following the pages.

        A real trip carries over a hundred events, so this pages on the
        first import rather than hypothetically.

        Args:
            trip_id: The trip to read from.
            collection: One of the plural collection names.
            updated_since: Only objects touched since this instant.
            deleted: Return tombstones instead of live objects.
            fields: Only these fields.
            exclude: Everything but these fields.
            filters: Route-specific filters passed through as given, for
                example `activityType` or `transportationType`.

        Yields:
            One object at a time, across every page.
        """
        path = f"/v2/trip/{trip_id}/{_collection(collection)}"
        yield from self.paginate(
            path,
            query(
                fields=fields,
                exclude=exclude,
                updatedSince=updated_since,
                deleted="true" if deleted else None,
                **filters,
            ),
        )

    # ==================================================================
    # Plumbing
    # ==================================================================

    ####################################################################
    #
    def paginate(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """
        Walk a paginated route to the end.

        The `next` link is followed by its path and query only, against
        this client's own base URL, so a link pointing somewhere else
        cannot redirect a run off the API it authenticated to.

        Args:
            path: The first page's path.
            params: Query parameters for the first page.  Later pages
                take theirs from the link, which carries these forward.

        Yields:
            Each result, page by page.

        Raises:
            APIError: A `next` link pointed back at the page it came
                from, which would page forever.
        """
        query_params: dict[str, Any] = dict(params or {})
        while True:
            body = self._json("GET", path, params=query_params) or {}
            yield from (dict(item) for item in body.get("results", []))

            following = body.get("next")
            if not following:
                return

            url = httpx.URL(str(following))
            if url.path == path and dict(url.params) == query_params:
                raise APIError(
                    f"pagination stalled: {path} links to itself",
                    status_code=200,
                    method="GET",
                    url=str(following),
                )
            path, query_params = url.path, dict(url.params)

    ####################################################################
    #
    def _write(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        key_must_exceed: int = 0,
    ) -> WriteResult:
        """
        POST a create and read the duplicate-suppression answer.

        A create that landed answers 201 with the object.  A duplicate
        identifier answers 200 with an empty body and creates nothing,
        which is the whole basis for re-running an import.

        Raises:
            APIError: A 201 arrived with no object in it.  That is a
                success this cannot describe, and reporting it as
                `AlreadyExists` would tell the caller nothing was
                written when something was.
        """
        body = dict(payload)
        response = self.request(
            "POST", path, json=body, key_must_exceed=key_must_exceed
        )
        if response.status_code == 200:
            return AlreadyExists(body.get("internal_identifier"))
        if not response.content:
            raise APIError(
                f"POST {path} answered {response.status_code} with no object",
                status_code=response.status_code,
                method="POST",
                url=str(response.request.url),
            )
        return Created(dict(response.json()))

    ####################################################################
    #
    def _detail(self, trip_id: int, collection: str, child_id: int) -> str:
        """The detail route for a child, which uses the singular name."""
        return (
            f"/v1/trip/{trip_id}/"
            f"{COLLECTIONS[_collection(collection)]}/{child_id}"
        )


####################################################################
#
def _collection(name: str) -> str:
    """
    Check a collection name against the ones that exist.

    Raises:
        ValueError: The name is not one of the API's collections.  A typo
            would otherwise become a 404 from a route that looks real.
    """
    if name not in COLLECTIONS:
        known = ", ".join(sorted(COLLECTIONS))
        raise ValueError(
            f"unknown collection {name!r}; expected one of {known}"
        )
    return name


####################################################################
#
def query(
    *,
    fields: list[str] | None = None,
    exclude: list[str] | None = None,
    **extra: Any,
) -> dict[str, str]:
    """
    Build a query string, dropping anything that was not asked for.

    Args:
        fields: Restrict the response to these fields.
        exclude: Drop these fields, sent as the API's `fields!`.
        extra: Any other parameters; those that are None are left out.

    Returns:
        Parameters ready to hand to httpx.
    """
    params: dict[str, str] = {
        name: str(value) for name, value in extra.items() if value is not None
    }
    if fields:
        params["fields"] = ",".join(fields)
    if exclude:
        params["fields!"] = ",".join(exclude)
    return params
