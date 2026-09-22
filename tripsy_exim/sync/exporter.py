#!/usr/bin/env python
#
"""
Dated exports: what Tripsy held at one instant, written whole.

An export is not the staging archive.  The staging archive accumulates --
every write merges onto what is already there, because any response can be
partial -- and it is keyed by minted identifiers so a write is idempotent.
An export accumulates nothing.  Each run writes a fresh directory that
reads on its own, and two runs are never compared.

Layout under one run:

    <root>/exports/<stamp>/
      manifest.json                  when, and what was asked for
      <start>--<end>--<id>/
        trip.json                    the trip and all its children
        documents/<id>-<title>       whatever was attached, as it was
      quarantine/<key>.json          payloads the models would not take

A trip is one document rather than a file per object, because an export is
read by a person opening it or a program loading it whole, never by this
tool looking one object up.  The staging archive is the other way round,
which is why it keeps the other shape.

Trips come from the v2 list route, which is complete except for `emails`
and `collaborators_count` -- and those it does not send at all.  Skipping
`emails` is therefore the default by construction; asking for them costs a
detail request per trip.

A run builds into a sibling directory and moves it into place, so an
interrupted run leaves nothing a reader could mistake for a finished
export.  That also keeps each export a fresh directory, which is what
stops `Archive`-style merging from turning a point in time into an
accumulation.
"""

# system imports
import errno
import fcntl
import re
import shutil
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Protocol

# 3rd party imports
import httpx

# Project imports
from tripsy_exim.api import TripsyClient
from tripsy_exim.models.activity import Activity
from tripsy_exim.models.base import CanonicalModel
from tripsy_exim.models.collaborator import Collaborator
from tripsy_exim.models.expense import Expense
from tripsy_exim.models.hosting import Hosting
from tripsy_exim.models.transportation import Transportation
from tripsy_exim.models.trip import Trip
from tripsy_exim.store import quarantine_key, write_json

# Bumped when the shape of an export changes in a way a reader must know
# about.  Written into every manifest and every trip document.
#
EXPORT_SCHEMA_VERSION = 1

# One run's directory name.  Colon-free: a stamp is a directory name, and
# not every filesystem takes a colon in one.
#
STAMP_FORMAT = "%Y-%m-%dT%H%M%SZ"

# What a run builds into before it is moved into place.  Dot-prefixed so a
# listing of finished exports does not show a run still in flight.
#
PARTIAL = ".{stamp}.partial"

# The child collections that have a canonical model, in the order they are
# written.  `documents` is deliberately absent: see `_documents`.
#
MODELLED: dict[str, type[CanonicalModel]] = {
    "hostings": Hosting,
    "activities": Activity,
    "transportations": Transportation,
    "expenses": Expense,
    "collaborators": Collaborator,
}

DOCUMENTS = "documents"

# Held for the length of a run, so a scheduled export and one started by
# hand cannot write at the same moment.
#
LOCK = ".lock"

# Runs of anything that has no place in a filename.  Titles come from
# whoever uploaded the file, so they carry spaces, apostrophes and
# whatever else a person typed.
#
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


########################################################################
########################################################################
#
class Fetch(Protocol):
    """Anything that can turn a URL into bytes."""

    ####################################################################
    #
    def __call__(self, url: str) -> bytes:
        """Read the whole body at this URL."""
        ...


########################################################################
########################################################################
#
@dataclass(frozen=True)
class TripWritten:
    """One trip, just written: what a progress report is told."""

    payload: dict[str, Any]
    directory: Path
    objects: int
    documents: int


# Called once per trip as it is written.
#
Progress = Callable[[TripWritten], None]


########################################################################
########################################################################
#
@dataclass
class ExportOutcome:
    """
    What one export run wrote.

    `objects` counts child objects written into a trip document, so it
    excludes both the documents saved beside them and anything
    quarantined -- three numbers a summary line has to keep apart rather
    than invite a reader to reconcile.
    """

    path: Path
    trips: int = 0
    objects: int = 0
    documents: int = 0
    quarantined: list[str] = field(default_factory=list)


########################################################################
########################################################################
#
@dataclass(frozen=True)
class Selection:
    """
    Which trips a run was asked for.

    Name selectors union: naming two trips exports both, and a glob that
    matches neither still lets a named one through.  The date range then
    narrows whatever that came to, so `--glob 'Japan*' --from 2020-01-01`
    reads as "the Japan trips, from 2020 on" rather than as two separate
    requests.

    Asking for nothing at all is refused.  A backup command whose bare
    form silently meant "everything" would eventually be run by somebody
    who meant something narrower.
    """

    everything: bool = False
    trips: tuple[str, ...] = ()
    glob: str | None = None
    since: date | None = None
    until: date | None = None

    ####################################################################
    #
    def __post_init__(self) -> None:
        """
        Raises:
            ValueError: Nothing was asked for, or `everything` was asked
                for alongside a selector that narrows it by name.
        """
        named = bool(self.trips or self.glob)
        dated = self.since is not None or self.until is not None

        if not (self.everything or named or dated):
            raise ValueError(
                "nothing selected: name trips with --trip or --glob, "
                "give a date range, or ask for --all"
            )
        if self.everything and named:
            raise ValueError(
                "--all covers every trip; it cannot be combined with "
                "--trip or --glob"
            )
        if self.since and self.until and self.since > self.until:
            raise ValueError(f"{self.since} is after {self.until}")

    ####################################################################
    #
    @property
    def scope(self) -> dict[str, Any]:
        """
        What to record in the manifest.

        A partial export has to say it is one: a directory that cannot
        tell a reader whether it means "everything Tripsy held" or
        "these trips" is read wrongly by whoever restores from it.

        Returns:
            The selection, JSON-ready.
        """
        return {
            "all": self.everything and not self.narrowed,
            "trips": list(self.trips),
            "glob": self.glob,
            "from": self.since.isoformat() if self.since else None,
            "to": self.until.isoformat() if self.until else None,
        }

    ####################################################################
    #
    @property
    def narrowed(self) -> bool:
        """Whether anything at all limits what this run covers."""
        return bool(
            self.trips
            or self.glob
            or self.since is not None
            or self.until is not None
        )

    ####################################################################
    #
    def matches(self, trip: dict[str, Any]) -> bool:
        """
        Whether one trip is in this selection.

        Args:
            trip: The trip payload as the API sent it.

        Returns:
            Whether to export it.
        """
        if not self._named(trip):
            return False
        return self._dated(trip)

    ####################################################################
    #
    def _named(self, trip: dict[str, Any]) -> bool:
        """Whether a name selector lets this trip through."""
        if self.everything or not (self.trips or self.glob):
            return True

        name = str(trip.get("name") or "")
        folded = name.casefold()

        for wanted in self.trips:
            # A number is the Tripsy id, which is what a script has to
            # hand; anything else is part of a name, which is what a
            # person has.  Compared as text so an id never matches a
            # name that merely contains the digits.
            #
            if wanted.isdigit():
                if str(trip.get("id")) == wanted:
                    return True
            elif wanted.casefold() in folded:
                return True

        # `fnmatchcase` against folded text rather than `fnmatch`, whose
        # case handling follows the filesystem -- the same pattern would
        # otherwise mean different things on different machines.
        #
        return bool(self.glob) and fnmatchcase(
            folded, str(self.glob).casefold()
        )

    ####################################################################
    #
    def _dated(self, trip: dict[str, Any]) -> bool:
        """
        Whether the date range lets this trip through.

        A range asks when somebody travelled, so it matches on overlap:
        a fortnight abroad is in "December" if any of it was.  A trip
        with no dates has no answer to that question and is left out
        rather than guessed at -- `--all` is how to take those too.
        """
        if self.since is None and self.until is None:
            return True

        starts, ends = _travel_dates(trip)
        if starts is None or ends is None:
            return False

        if self.until is not None and starts > self.until:
            return False
        return not (self.since is not None and ends < self.since)

    ####################################################################
    #
    def select(self, trips: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        The trips this selection covers.

        Args:
            trips: Every trip the account holds.

        Returns:
            Those to export, in the order they arrived.
        """
        return [trip for trip in trips if self.matches(trip)]


####################################################################
#
def _travel_dates(trip: dict[str, Any]) -> tuple[date | None, date | None]:
    """
    When a trip happened, or nothing if it does not say.

    `has_dates` is authoritative: a trip saying it has none is treated
    as undated whatever the date fields hold.

    Args:
        trip: The trip payload as the API sent it.

    Returns:
        The first and last day, or two Nones.
    """
    if trip.get("has_dates") is False:
        return None, None
    try:
        return (
            date.fromisoformat(str(trip["starts_at"])),
            date.fromisoformat(str(trip["ends_at"])),
        )
    except KeyError, TypeError, ValueError:
        return None, None


####################################################################
#
@contextmanager
def only_one_run(exports_root: Path) -> Iterator[None]:
    """
    Hold the exports directory for the length of one run.

    A scheduled export and one started by hand can land together, and
    two runs writing at once would race for the same stamp.  The lock is
    advisory and held on an open file descriptor, so it goes away with
    the process however the process ends -- a stale lock file cannot
    wedge the next run.

    Args:
        exports_root: The directory runs accumulate in.

    Yields:
        Nothing; the lock is held for the body.

    Raises:
        BlockingIOError: Another run holds it.
    """
    exports_root.mkdir(parents=True, exist_ok=True)
    handle = (exports_root / LOCK).open("w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise BlockingIOError(
                    f"another export is running in {exports_root}"
                ) from exc
            raise
        yield
    finally:
        handle.close()


####################################################################
#
def stamp_for(when: datetime | None = None) -> str:
    """
    The directory name a run writes under.

    Args:
        when: The instant to name the run after.  Defaults to now.

    Returns:
        A UTC timestamp, safe as a directory name.
    """
    moment = when or datetime.now(UTC)
    return moment.astimezone(UTC).strftime(STAMP_FORMAT)


####################################################################
#
def trip_directory(trip: dict[str, Any]) -> str:
    """
    The directory one trip is written into.

    Named to sort by travel date, which is the order somebody reading a
    backup wants, with the Tripsy id making it unique -- two trips can
    share a name, and a pair recording one journey twice is exactly what
    `merge` exists for.

    `has_dates` is authoritative: a trip that says it has none is named
    without them even when the fields are populated.

    Args:
        trip: The trip payload as the API sent it.

    Returns:
        A single directory component.
    """
    trip_id = trip.get("id")
    if trip.get("has_dates") is False:
        return f"undated--{trip_id}"

    starts, ends = trip.get("starts_at"), trip.get("ends_at")
    if not starts or not ends:
        return f"undated--{trip_id}"
    return f"{starts}--{ends}--{trip_id}"


####################################################################
#
def document_filename(document: dict[str, Any]) -> str:
    """
    What one attached file is saved as.

    The id leads so two files of the same name cannot collide, and the
    title follows so a person can tell them apart.  The title carries
    its own extension and keeps it: that is what makes the saved file
    open in the right thing.

    Args:
        document: The document payload as the API sent it.

    Returns:
        A single filename component.
    """
    title = _UNSAFE.sub("_", str(document.get("title") or "")).strip("._-")
    return f"{document['id']}-{title}" if title else str(document["id"])


####################################################################
#
def default_fetch(url: str) -> bytes:
    """
    Read a URL with no Tripsy credentials attached.

    A document's `temp_read_url` is pre-signed, so it authenticates
    itself and is not a Tripsy API call: sending the token would be
    pointless, and the API's pacing profile is calibrated for the API
    rather than for pulling an object out of a bucket.

    Args:
        url: The pre-signed URL.

    Returns:
        The whole body.
    """
    response = httpx.get(url, follow_redirects=True, timeout=30.0)
    response.raise_for_status()
    return response.content


####################################################################
#
def export(
    client: TripsyClient,
    exports_root: Path,
    trips: Iterable[dict[str, Any]],
    *,
    scope: dict[str, Any],
    when: datetime | None = None,
    fetch: Fetch | None = None,
    progress: Progress | None = None,
) -> ExportOutcome:
    """
    Write one dated export.

    The run builds into a sibling directory and is moved into place at
    the end, so what appears under a stamp is always a finished export.
    Two runs in the same second would name the same directory; the move
    then fails rather than merging, and a lock is what keeps two runs
    from trying.

    The caller holds `only_one_run` for the call.  A run starts by
    removing whatever interrupted runs left behind, which is only safe
    because no other run can be building.

    Args:
        client: An authenticated client.
        exports_root: The directory runs accumulate in.
        trips: Trip payloads to write, already selected.
        scope: What was asked for, recorded verbatim in the manifest so
            a partial export says it is one.
        when: The instant to name the run after.  Defaults to now.
        fetch: How to read a document's bytes.  Defaults to an
            unauthenticated request.
        progress: Told about each trip as soon as it is written, so a
            long run can report as it goes.

    Returns:
        What the run wrote.

    Raises:
        FileExistsError: An export of that name is already there.
    """
    stamp = stamp_for(when)
    final = exports_root / stamp
    building = exports_root / PARTIAL.format(stamp=stamp)

    if final.exists():
        raise FileExistsError(f"an export already exists at {final}")

    # Every directory left by an interrupted run is removed, whatever
    # instant it was for: what is in one was never finished, and nothing
    # else can be building while the caller holds the lock.
    #
    if exports_root.is_dir():
        for leftover in exports_root.glob(PARTIAL.format(stamp="*")):
            shutil.rmtree(leftover)
    building.mkdir(parents=True)

    outcome = ExportOutcome(path=final)
    written: list[dict[str, Any]] = []

    for payload in trips:
        trip = _validated(Trip, payload, building, outcome)
        if trip is None:
            continue

        directory = building / trip_directory(payload)
        directory.mkdir()

        documents_before = outcome.documents
        document, objects = _trip_document(
            client, payload, directory, outcome, fetch or default_fetch
        )
        write_json(directory / "trip.json", document)

        outcome.trips += 1
        outcome.objects += objects
        if progress is not None:
            progress(
                TripWritten(
                    payload=payload,
                    directory=directory,
                    objects=objects,
                    documents=outcome.documents - documents_before,
                )
            )
        written.append(
            {
                "id": payload.get("id"),
                "name": payload.get("name"),
                "directory": directory.name,
            }
        )

    write_json(
        building / "manifest.json",
        {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "exported_at": stamp,
            "scope": scope,
            "trips": written,
        },
    )
    building.rename(final)
    return outcome


####################################################################
#
def _trip_document(
    client: TripsyClient,
    payload: dict[str, Any],
    directory: Path,
    outcome: ExportOutcome,
    fetch: Fetch,
) -> tuple[dict[str, Any], int]:
    """
    Build one trip's document and save whatever is attached to it.

    Args:
        client: An authenticated client.
        payload: The trip payload as the API sent it.
        directory: Where this trip is being written.
        outcome: Counters to add to.
        fetch: How to read a document's bytes.

    Returns:
        The document, and how many child objects it carries.
    """
    trip_id = int(payload["id"])
    children: dict[str, list[dict[str, Any]]] = {}
    objects = 0

    for collection, model in MODELLED.items():
        rows = []
        for child in client.iter_children(trip_id, collection):
            validated = _validated(model, child, directory.parent, outcome)
            if validated is not None:
                rows.append(validated.model_dump(mode="json"))
        if rows:
            children[collection] = rows
            objects += len(rows)

    attached = _documents(client, trip_id, directory, outcome, fetch)
    if attached:
        children[DOCUMENTS] = attached

    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "trip": payload,
        **children,
    }, objects


####################################################################
#
def _documents(
    client: TripsyClient,
    trip_id: int,
    directory: Path,
    outcome: ExportOutcome,
    fetch: Fetch,
) -> list[dict[str, Any]]:
    """
    Save a trip's attached files and describe them.

    Downloaded here, one trip at a time, because `temp_read_url` is
    pre-signed and expires: a run that listed every trip first and
    fetched afterwards would be racing its own URLs.  The URL is then
    dropped from what is recorded -- storing a link that is dead by the
    time anybody reads it is worse than storing nothing.

    There is no canonical model for a document, so the payload is kept
    as it arrived.  The three link arrays say which activity, hosting or
    transportation a file belongs to, and are the reason a document is
    not simply a file on the trip.

    Args:
        client: An authenticated client.
        trip_id: The trip to read from.
        directory: Where this trip is being written.
        outcome: Counters to add to.
        fetch: How to read a document's bytes.

    Returns:
        One entry per document, in the order the API listed them.
    """
    described: list[dict[str, Any]] = []
    for document in client.iter_children(trip_id, DOCUMENTS):
        url = document.get("temp_read_url")
        recorded = {k: v for k, v in document.items() if k != "temp_read_url"}

        if url:
            saved = directory / DOCUMENTS / document_filename(document)
            saved.parent.mkdir(exist_ok=True)
            saved.write_bytes(fetch(str(url)))
            recorded["file"] = f"{DOCUMENTS}/{saved.name}"
            outcome.documents += 1

        described.append(recorded)
    return described


####################################################################
#
def _validated(
    model: type[CanonicalModel],
    payload: dict[str, Any],
    root: Path,
    outcome: ExportOutcome,
) -> Any:
    """
    Parse one payload, keeping it verbatim if it will not parse.

    `Archive.ingest` does this for the staging archive, but it writes one
    file per object where an export writes one per trip, so the keeping
    is done here against the export's own layout.

    Args:
        model: The canonical model the payload should become.
        payload: The raw JSON object as received.
        root: The export being built, which holds `quarantine/`.
        outcome: Counters to add to.

    Returns:
        The parsed object, or None when it was kept instead.
    """
    try:
        return model.model_validate(payload)
    except ValueError as error:
        # `quarantine_key` rather than the id, because a payload that
        # will not parse may carry no id either -- and two of those
        # would otherwise be written to the same name, which is the one
        # thing quarantine exists to prevent.
        #
        key = f"{model.__name__}-{quarantine_key(payload)}"
        write_json(
            root / "quarantine" / f"{key}.json",
            {
                "kind": model.__name__,
                "reason": str(error),
                "payload": payload,
            },
        )
        outcome.quarantined.append(key)
        return None
