#!/usr/bin/env python
#
"""
Command line entry point for tripsy-exim.

Subcommands are added by the import, export, and status work; this module
owns only the command group and the options common to every subcommand.

This is also the single place credentials are resolved.  Nothing else in
the package reads the environment, runs `op`, or holds a password: the
token from `POST /auth` is passed down to the API client and lives in
memory for the run.
"""

# system imports
import os
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# 3rd party imports
import click
from dotenv import find_dotenv, load_dotenv

# Project imports
from tripsy_exim import __version__
from tripsy_exim.api import (
    IMPORT,
    INTERACTIVE,
    PacingProfile,
    TokenAuth,
    TripsyClient,
)
from tripsy_exim.geocode import (
    Cache,
    far_from,
    lookup_for,
    placed,
    plausible,
)
from tripsy_exim.models import scratch_namespace
from tripsy_exim.secrets import (
    PASSWORD,
    SECRET_URL_ENV,
    TOKEN,
    USERNAME,
    SecretError,
    SecretStore,
    store_for,
)
from tripsy_exim.store import ARCHIVE_ENV, Archive, default_root
from tripsy_exim.sync import TRIP_INDEX, stage_export_file, stage_file
from tripsy_exim.sync.importer import (
    declare_merge,
    in_travel_order,
    merged_into,
    plan_trip,
    positions_in,
    resolve_trip_key,
    staged_trip,
    undo_merge,
    unplaced_in,
    upload_trip,
    uploaded_trips,
    verify_trip,
)


########################################################################
#
@click.group()
@click.version_option(version=__version__, prog_name="tripsy-exim")
def main() -> None:
    """Export and import trip data for Tripsy.app."""
    # Before any command reads the environment, and before a default is
    # resolved from it.  Searched from the working directory upwards, so
    # running from anywhere inside a project finds that project's file
    # rather than one beside the installed package.
    #
    load_dotenv(find_dotenv(usecwd=True))


########################################################################
########################################################################
#
# Credentials
#
# Resolved here and nowhere else.  Nothing below this module reads the
# environment, runs a secret store, or holds a password: the token from
# POST /auth is handed to the client and lives in memory for the run.
#


####################################################################
#
def resolve_credentials(
    username: str | None, password: str | None, store: SecretStore | None
) -> tuple[str, str]:
    """
    Settle which credentials a run authenticates with.

    First match wins: a command line flag, then the environment, then
    `.env`, then the secret store.  The store is last because it is the
    one that keeps a plaintext password out of the environment entirely,
    so anything more explicit is a deliberate override of it.

    Args:
        username: Username given on the command line, or None.
        password: Password given on the command line, or None.
        store: The configured secret store, or None.

    Returns:
        The username and password to authenticate with.

    Raises:
        click.ClickException: Nothing resolved to a usable pair.
    """
    username = username or os.environ.get("TRIPSY_USERNAME")
    password = password or os.environ.get("TRIPSY_PASSWORD")
    if username and password:
        return username, password

    if store is not None:
        username = username or store.get(USERNAME)
        password = password or store.get(PASSWORD)
        if username and password:
            return username, password

    raise click.ClickException(
        "no credentials: pass --username/--password, set TRIPSY_USERNAME "
        f"and TRIPSY_PASSWORD, or point {SECRET_URL_ENV} at a record "
        "holding them"
    )


####################################################################
#
@contextmanager
def open_session(
    username: str | None,
    password: str | None,
    profile: PacingProfile = IMPORT,
) -> Iterator[TripsyClient]:
    """
    Yield a client authenticated however this run can manage it.

    A store that can write caches the token beside the password, so a run
    that follows a successful one sends no credentials at all.  The token
    has no stated lifetime -- these are DRF tokens, which are not
    documented to expire -- so the only way to learn a cached one is spent
    is to be refused, and that refusal is what replaces it.

    Args:
        username: Username given on the command line, or None.
        password: Password given on the command line, or None.
        profile: Pacing profile for the run.

    Yields:
        A client carrying a token, which re-authenticates once if that
        token turns out to be spent.
    """
    try:
        store = store_for()
    except SecretError as exc:
        raise click.ClickException(str(exc)) from exc

    ####################################################################
    #
    def fresh() -> str:
        """Trade credentials for a token, caching it where possible."""
        name, secret = resolve_credentials(username, password, store)
        with TripsyClient(profile=INTERACTIVE) as session:
            token = session.login(name, secret)
        if store is not None and store.writable:
            try:
                store.put(TOKEN, token)
            except SecretError as exc:
                # Not fatal: the run has a token and only the saving of a
                # request next time is lost.
                #
                click.echo(f"could not cache the token: {exc}", err=True)
        return token

    cached = None
    if store is not None:
        try:
            cached = store.get(TOKEN)
        except SecretError as exc:
            raise click.ClickException(str(exc)) from exc

    client = TripsyClient(
        profile=profile,
        auth=TokenAuth(cached or fresh()),
        reauthenticate=fresh,
    )
    try:
        yield client
    finally:
        client.close()


####################################################################
#
def archive_for(archive_root: Path | None) -> Path:
    """
    Settle which directory holds the archive.

    A flag wins, then `TRIPSY_EXIM_ARCHIVE`, then the XDG data
    directory.  A leading `~` is expanded wherever the value came from:
    a shell expands one on the command line, but nothing expands one
    written in `.env` or exported with quotes.

    Args:
        archive_root: The directory named on the command line, or None.

    Returns:
        The directory to read and write.
    """
    if archive_root is not None:
        return Path(archive_root).expanduser()
    return default_root()


####################################################################
#
def staged_archive(archive_root: Path | None) -> Archive:
    """
    Open an archive that is expected to already hold trips.

    Args:
        archive_root: The directory named on the command line, or None.

    Returns:
        The archive.

    Raises:
        click.ClickException: There is no such directory.  The resolved
            path is named, since it may have come from `.env` or a
            default rather than from the command line.
    """
    root = archive_for(archive_root)
    if not root.is_dir():
        raise click.ClickException(f"no archive directory at {root}")

    # Being able to name a directory is not being able to read it.  On
    # macOS a folder under Documents or Desktop is refused to a process
    # the system has not been told to trust, and a scheduled run has no
    # one to ask, so the refusal is reported rather than raised as a
    # stack trace from inside a directory walk.
    #
    archive = Archive(root)
    try:
        archive.trip_keys()
    except OSError as exc:
        raise click.ClickException(
            f"cannot read the archive at {root}: {exc.strerror or exc}"
        ) from exc
    return archive


####################################################################
#
def resolve_namespace(namespace: str | None, scratch: bool) -> str | None:
    """
    Settle which identifier namespace a staging run mints into.

    Args:
        namespace: A namespace named on the command line, or None.
        scratch: Whether a fresh throwaway namespace was asked for.

    Returns:
        The namespace to mint into, or None for the parser's default.

    Raises:
        click.UsageError: Both were given, which cannot be honoured.
    """
    if scratch and namespace is not None:
        raise click.UsageError(
            "--scratch mints its own namespace; pass one or the other"
        )
    if scratch:
        namespace = scratch_namespace()
        click.echo(f"scratch namespace: {namespace}")
    return namespace


####################################################################
#
@main.command()
@click.argument(
    "sources",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--archive",
    "archive_root",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help=(
        "Directory the canonical objects are written to.  Defaults to "
        f"${ARCHIVE_ENV}, or ~/.local/share/tripsy-exim/archive."
    ),
)
@click.option(
    "--namespace",
    default=None,
    help="Identifier namespace to mint into.  Implies a shaping run.",
)
@click.option(
    "--scratch",
    is_flag=True,
    default=False,
    help=(
        "Mint into a fresh throwaway namespace, so the run can be deleted "
        "and redone without spending the real identifiers."
    ),
)
def stage(
    sources: tuple[Path, ...],
    archive_root: Path | None,
    namespace: str | None,
    scratch: bool,
) -> None:
    """
    Parse .ics files into the local archive.

    Nothing is sent to Tripsy.  Each calendar becomes canonical objects on
    disk, with the parser's report beside the trip, ready to be reviewed
    and corrected before anything is posted.
    """
    namespace = resolve_namespace(namespace, scratch)

    archive_root = archive_for(archive_root)
    archive = Archive(archive_root)
    total = 0
    for source in sources:
        try:
            staged = stage_file(archive, source, namespace)
        except ValueError as exc:
            click.echo(f"{source.name}: not a parseable calendar: {exc}")
            continue
        total += staged.total
        counts = ", ".join(
            f"{n} {name}" for name, n in staged.counts.items() if n
        )
        click.echo(f"{source.name} -> {staged.trip_key} ({counts or 'empty'})")

    plural = "" if len(sources) == 1 else "s"
    click.echo(
        f"\n{len(sources)} file{plural}, {total} objects into {archive_root}"
    )


####################################################################
#
@main.command("stage-export")
@click.argument(
    "export",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--archive",
    "archive_root",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help=(
        "Directory the canonical objects are written to.  Defaults to "
        f"${ARCHIVE_ENV}, or ~/.local/share/tripsy-exim/archive."
    ),
)
@click.option(
    "--namespace",
    default=None,
    help="Identifier namespace to mint into.  Implies a shaping run.",
)
@click.option(
    "--scratch",
    is_flag=True,
    default=False,
    help=(
        "Mint into a fresh throwaway namespace, so the run can be deleted "
        "and redone without spending the real identifiers."
    ),
)
def stage_export_command(
    export: Path,
    archive_root: Path | None,
    namespace: str | None,
    scratch: bool,
) -> None:
    """
    Parse a TripIt GDPR export into the local archive.

    Nothing is sent to Tripsy.  One export carries a whole account, so
    every trip in it is staged in one pass, each with the reader's report
    beside it, ready to be reviewed and corrected before anything is
    posted.

    The export is authoritative for a trip's identity, so stage it before
    any calendars: a `.ics` naming a trip already staged from here is
    refused rather than staged a second time.
    """
    namespace = resolve_namespace(namespace, scratch)

    archive_root = archive_for(archive_root)
    archive = Archive(archive_root)
    try:
        staged = stage_export_file(archive, export, namespace)
    except ValueError as exc:
        raise click.ClickException(
            f"{export.name}: not a parseable export: {exc}"
        ) from exc

    totals: Counter[str] = Counter()
    for trip in staged:
        totals.update(trip.counts)
        counts = ", ".join(
            f"{n} {name}" for name, n in trip.counts.items() if n
        )
        click.echo(f"  {trip.trip_key} ({counts or 'empty'})")

    objects = sum(totals.values())
    plural = "" if len(staged) == 1 else "s"
    summary = ", ".join(
        f"{n} {name}" for name, n in sorted(totals.items()) if n
    )
    click.echo(
        f"\n{len(staged)} trip{plural}, {objects} objects into {archive_root}"
    )
    if summary:
        click.echo(f"  {summary}")


####################################################################
#
@main.command("list")
@click.option(
    "--archive",
    "archive_root",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help=(
        "Directory the staged trips are read from.  Defaults to "
        f"${ARCHIVE_ENV}, or ~/.local/share/tripsy-exim/archive."
    ),
)
@click.option(
    "--pending/--all",
    default=False,
    help="List only trips no run has finished uploading.",
)
def list_command(archive_root: Path | None, pending: bool) -> None:
    """
    List the trips staged in the archive, oldest first.

    The mark in the first column says whether a run has finished
    uploading that trip.
    """
    archive = staged_archive(archive_root)
    archive_root = archive.root
    keys = in_travel_order(archive, archive.trip_keys())
    if not keys:
        raise click.ClickException(f"no staged trips in {archive_root}")

    done = uploaded_trips(archive)
    shown = 0
    for key in keys:
        if pending and key in done:
            continue
        shown += 1
        trip = staged_trip(archive, key)
        name = str(getattr(trip, "name", "") or key)
        starts = getattr(trip, "starts_at", None)
        mark = "up" if key in done else "  "
        if key in merged_into(archive):
            mark = "->"
        click.echo(f"  {mark}  {str(starts or ''):10}  {name[:48]:48} {key}")

    click.echo(f"\n{shown} trips, {len(done)} already uploaded")


####################################################################
#
@main.command("merge")
@click.argument("absorbed")
@click.argument("target", required=False)
@click.option(
    "--undo",
    is_flag=True,
    default=False,
    help="Release ABSORBED so it uploads as its own trip again.",
)
@click.option(
    "--archive",
    "archive_root",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help=(
        "Directory the staged trips are read from.  Defaults to "
        f"${ARCHIVE_ENV}, or ~/.local/share/tripsy-exim/archive."
    ),
)
def merge_command(
    absorbed: str,
    target: str | None,
    archive_root: Path | None,
    undo: bool,
) -> None:
    """
    Upload one staged trip as part of another.

    One journey can reach the archive as two trips: the export records a
    trip per traveller, so a holiday taken together arrives twice, each
    copy holding that traveller's own flights and room.  Uploading both
    makes two rival trips out of one journey.

    Nothing moves on disk.  Both trips stay as the parser produced them,
    so staging stays lossless and the declaration can be undone; it is
    read when uploading and nowhere else.

    Trips are named by key, not by name: the trips this is for share a
    name, which is how they were found in the first place.
    """
    archive = staged_archive(archive_root)

    if undo:
        try:
            undo_merge(archive, absorbed)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"{absorbed} uploads as its own trip again")
        return

    if target is None:
        raise click.UsageError("name the trip to merge into, or pass --undo")

    try:
        declare_merge(archive, absorbed, target)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    plan = plan_trip(archive, target)
    click.echo(f"{absorbed}\n  uploads as part of\n{target}")
    click.echo(f"\n{plan.name}: {plan.total} objects after the merge")
    if plan.duplicates:
        click.echo(
            f"  {plan.duplicates} objects the target already held, not sent "
            f"again"
        )


####################################################################
#
@main.command("upload")
@click.option(
    "--archive",
    "archive_root",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help=(
        "Directory the staged trips are read from.  Defaults to "
        f"${ARCHIVE_ENV}, or ~/.local/share/tripsy-exim/archive."
    ),
)
@click.option(
    "--trip",
    "wanted",
    multiple=True,
    help=(
        "Upload only this trip, named by part of its name or by its key.  "
        "Repeatable; default is every trip."
    ),
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help=(
        "Upload at most this many trips, oldest first.  Trips an earlier "
        "run finished do not count against it."
    ),
)
@click.option(
    "--write/--dry-run",
    default=False,
    help=(
        "Actually send to Tripsy.  The default plans and prints without "
        "writing, since an identifier Tripsy has seen is never released."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Include trips an earlier run already finished.",
)
@click.option("--username", default=None, help="Tripsy account username.")
@click.option("--password", default=None, help="Tripsy account password.")
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="List every object a trip would write, not just the totals.",
)
def upload_command(
    archive_root: Path | None,
    wanted: tuple[str, ...],
    limit: int | None,
    write: bool,
    force: bool,
    username: str | None,
    password: str | None,
    verbose: bool,
) -> None:
    """
    Upload staged trips from the archive to Tripsy.

    Plans first and prints the plan.  Without `--write` that is all it
    does: nothing is sent, no credentials are needed, and the numbers
    shown are the ones a real run would send.

    Trips go oldest first, so `--limit` works forward through an account
    rather than picking an arbitrary handful.  A trip an earlier run
    finished is stepped over without a request and does not count against
    `--limit`, so running with `--limit 1` repeatedly walks the archive a
    trip at a time.

    Re-running is a no-op rather than a source of duplicates, so a run
    that failed part way is resumed by running it again.
    """
    archive = staged_archive(archive_root)
    archive_root = archive.root

    if wanted:
        try:
            keys = [resolve_trip_key(archive, needle) for needle in wanted]
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        keys = archive.trip_keys()
    if not keys:
        raise click.ClickException(f"no staged trips in {archive_root}")

    # A trip declared as part of another is not uploaded in its own
    # right; its objects go up with the trip that absorbs it.
    #
    absorbed = merged_into(archive)
    merged = [key for key in keys if key in absorbed]
    keys = [key for key in keys if key not in absorbed]

    keys = in_travel_order(archive, keys)

    done = uploaded_trips(archive)
    skipped = 0
    if not force:
        before = len(keys)
        keys = [key for key in keys if key not in done]
        skipped = before - len(keys)
    if limit is not None:
        keys = keys[:limit]

    if not keys:
        click.echo(
            f"Nothing to upload: {skipped} trips already finished.  "
            "Pass --force to send them again."
        )
        return

    plans = [plan_trip(archive, key) for key in keys]
    objects = sum(plan.total for plan in plans)
    untyped = sum(len(plan.untyped) for plan in plans)

    for plan in plans:
        click.echo(f"\n{plan.name or plan.trip_key}  [{plan.trip_key}]")
        click.echo(f"  {plan.total} objects")
        if verbose:
            for obj in plan.objects:
                kind = obj.type_value or "-"
                click.echo(
                    f"    {obj.sort_order:>4}  {obj.when:16} "
                    f"{obj.collection[:3]}  {kind:10} {obj.name[:40]}"
                )
        if plan.untyped:
            click.echo(f"  {len(plan.untyped)} untyped:")
            for obj in plan.untyped:
                click.echo(f"      {obj.when:16} {obj.name[:48]}")

    click.echo(
        f"\n{len(plans)} trips, {objects} objects, {untyped} untyped legs"
    )
    if skipped:
        click.echo(f"{skipped} trips skipped, already uploaded")
    if merged:
        click.echo(f"{len(merged)} trips merged into another and not created")

    # Two staged trips sharing a name and a date range are either one
    # journey recorded twice or one journey recorded per traveller.  A
    # per-trip plan cannot show it, and uploading both makes two rival
    # trips out of what the app should hold as one.
    #
    planned = {plan.trip_key for plan in plans}
    index = archive.read_manifest().get(TRIP_INDEX) or {}
    for join_key, shared_keys in sorted(index.items()):
        shared = [
            key for key in shared_keys if key in planned and key not in absorbed
        ]
        if len(shared) > 1:
            click.echo(f"\n  NOTE: {len(shared)} trips share one key:")
            click.echo(f"      {join_key}")
            for key in shared:
                click.echo(f"      {key}")

    if not write:
        click.echo("\nDry run.  Nothing was sent.  Pass --write to upload.")
        return

    # Tripsy requires a transportation to say what kind it is, so an
    # untyped leg is refused one object at a time in the middle of a run,
    # leaving its trip short.  Better to say so before anything is sent.
    #
    if untyped:
        raise click.ClickException(
            f"{untyped} legs carry no transportation_type, which Tripsy "
            f"requires.  They would be refused and their trips would go up "
            f"incomplete.  Type them, or select trips that have none."
        )

    created = existing = 0
    failures: list[str] = []
    with open_session(username, password) as client:
        for plan in plans:
            result = upload_trip(client, archive, plan.trip_key)
            created += result.created
            existing += result.existing
            failures.extend(result.failed)
            mark = "created" if result.trip_created else "existing"
            click.echo(
                f"{plan.name[:40]:40} {mark:8} "
                f"+{result.created} ={result.existing}"
                f"{f' !{len(result.failed)}' if result.failed else ''}"
            )

    click.echo(f"\n{created} created, {existing} already there")
    if failures:
        click.echo(f"{len(failures)} failed:")
        for failure in failures[:20]:
            click.echo(f"  {failure}")
        raise click.ClickException(f"{len(failures)} objects failed")


####################################################################
#
@main.command("verify")
@click.option(
    "--archive",
    "archive_root",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help=(
        "Directory the staged trips are read from.  Defaults to "
        f"${ARCHIVE_ENV}, or ~/.local/share/tripsy-exim/archive."
    ),
)
@click.option(
    "--trip",
    "wanted",
    multiple=True,
    help=(
        "Check only this trip, named by part of its name or by its key.  "
        "Repeatable; default is every trip an earlier run finished."
    ),
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Check at most this many trips, oldest first.",
)
@click.option("--username", default=None, help="Tripsy account username.")
@click.option("--password", default=None, help="Tripsy account password.")
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="List every object the account holds that the plan does not.",
)
def verify_command(
    archive_root: Path | None,
    wanted: tuple[str, ...],
    limit: int | None,
    username: str | None,
    password: str | None,
    verbose: bool,
) -> None:
    """
    Read uploaded trips back from Tripsy and compare them to the plan.

    Nothing is written.  This reports what is there against what was
    meant to be there: objects that never arrived, objects the plan does
    not know about, and any `sort_order` that does not match.

    It also lists objects carrying an address Tripsy has not yet resolved
    to a position.  Geocoding runs after the create, so a trip checked
    moments after uploading reads as unplaced and is worth checking again
    before anything is corrected.
    """
    archive = staged_archive(archive_root)

    if wanted:
        try:
            keys = [resolve_trip_key(archive, needle) for needle in wanted]
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        keys = sorted(uploaded_trips(archive))
    if not keys:
        raise click.ClickException(
            f"no trips have been uploaded from {archive.root}"
        )

    keys = in_travel_order(archive, keys)
    if limit is not None:
        keys = keys[:limit]

    agreed = 0
    unplaced = 0
    with open_session(username, password) as client:
        for key in keys:
            result = verify_trip(client, archive, key)
            unplaced += len(result.unplaced)
            mark = "ok" if result.agrees else "DIFFERS"
            extra = (
                f", {len(result.extra)} more in the account"
                if result.extra
                else ""
            )
            click.echo(
                f"\n{result.name or result.trip_key}  [{mark}]  "
                f"{result.matched} of {result.planned} planned{extra}"
            )
            if result.agrees:
                agreed += 1
            for identifier in result.missing:
                click.echo(f"    missing   {identifier}")
            for bad in result.differing:
                click.echo(
                    f"    {bad.field:10} {bad.name[:34]:34} "
                    f"planned {bad.planned!r}, found {bad.found!r}"
                )
            if result.extra and verbose:
                for identifier in result.extra:
                    click.echo(f"    not in the plan   {identifier}")
            if result.unplaced:
                click.echo(
                    f"    {len(result.unplaced)} addresses not yet placed"
                )
                if verbose:
                    for label in result.unplaced:
                        click.echo(f"        {label}")

    click.echo(f"\n{agreed} of {len(keys)} trips match their plan")
    if unplaced:
        click.echo(
            f"{unplaced} addresses carry no position yet.  Tripsy geocodes "
            "after the create, so check again before correcting any."
        )


####################################################################
#
@main.command("fix-locations")
@click.option(
    "--archive",
    "archive_root",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help=(
        "Directory the staged trips are read from, used to resolve "
        f"--trip.  Defaults to ${ARCHIVE_ENV}."
    ),
)
@click.option(
    "--trip",
    "wanted",
    multiple=True,
    help=(
        "Fix only this trip, named by part of its name or by its key.  "
        "Repeatable; default is every trip an earlier run uploaded."
    ),
)
@click.option(
    "--geocoder",
    default="nominatim",
    help="Which geopy service to ask.  Nominatim needs no account.",
)
@click.option(
    "--api-key",
    default=None,
    help="Key for a geocoder that wants one, e.g. opencage.",
)
@click.option(
    "--cache",
    "cache_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help=(
        "Where answers are kept between runs.  Defaults to "
        "~/.config/tripsy-exim/geocode.json.  Keeping them is a "
        "condition of Nominatim's terms, not an optimisation."
    ),
)
@click.option(
    "--far-km",
    default=2000.0,
    show_default=True,
    help=(
        "Refuse a result this many kilometres from everything else on "
        "its trip.  A geocoder fails by answering somewhere, not by "
        "answering nothing."
    ),
)
@click.option(
    "--forget",
    "forget",
    multiple=True,
    help=(
        "Drop this address from the cache and place it again, even "
        "where it already has a position.  Repeatable.  The way to "
        "correct an answer that was believed and should not have been, "
        "since nothing else would ever replace it."
    ),
)
@click.option(
    "--write/--dry-run",
    default=False,
    help="Actually send the positions to Tripsy.",
)
@click.option("--username", default=None, help="Tripsy account username.")
@click.option("--password", default=None, help="Tripsy account password.")
def fix_locations_command(
    archive_root: Path | None,
    wanted: tuple[str, ...],
    geocoder: str,
    api_key: str | None,
    cache_path: Path | None,
    far_km: float,
    forget: tuple[str, ...],
    write: bool,
    username: str | None,
    password: str | None,
) -> None:
    """
    Geocode the objects Tripsy left without a position.

    A clean-up run over trips already uploaded.  It finds every object
    carrying an address and no coordinates, looks the address up with an
    outside geocoding service, and sends the result back to Tripsy.

    Why any of that is needed: Tripsy geocodes an activity's address when
    the app renders it, and never geocodes a transportation endpoint at
    all -- not on create, not on update.  So a leg pins only if something
    hands it coordinates.  Activities the app has already resolved are
    skipped, so running this after looking at a trip leaves only the legs
    and whatever the app could not resolve either.

    It reads Tripsy rather than the archive: what wants placing is
    whatever the app has not placed, which only Tripsy knows.  Nothing is
    written back to the archive either -- the archive is what the export
    gives us, and this is a clean-up run over what was imported.

    Every answer is refused if it lands further than `--far-km` from
    everything else on its trip.  'Kyoto Station' resolves to a point in
    California, which is not distinguishable from a good answer except by
    measuring it against what the trip already knows.
    """
    archive = staged_archive(archive_root)

    if wanted:
        try:
            keys = [resolve_trip_key(archive, needle) for needle in wanted]
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        keys = sorted(uploaded_trips(archive))
    if not keys:
        raise click.ClickException(
            f"no trips have been uploaded from {archive.root}"
        )
    keys = in_travel_order(archive, keys)

    cache = Cache(cache_path)
    click.echo(f"cache: {cache.path}")
    for address in forget:
        dropped = "forgotten" if cache.forget(address) else "was not held"
        click.echo(f"  {address}: {dropped}")
    if forget:
        cache.save()

    with open_session(username, password) as client:
        ids = client.trip_ids_by_identifier()
        work: dict[str, tuple[int, list[Any]]] = {}
        references: dict[str, list[tuple[float, float]]] = {}
        for key in keys:
            trip = staged_trip(archive, key)
            trip_id = ids.get(str(trip.internal_identifier)) if trip else None
            if trip_id is None:
                continue
            gaps = unplaced_in(client, trip_id, forget)
            if gaps:
                work[key] = (trip_id, gaps)
                references[key] = positions_in(client, trip_id)

        addresses = [gap.address for _, gaps in work.values() for gap in gaps]
        unknown = [a for a in sorted(set(addresses)) if cache.get(a) is None]
        click.echo(
            f"{len(addresses)} endpoints, {len(set(addresses))} distinct "
            f"addresses, {len(unknown)} to look up"
        )
        if unknown and not write:
            click.echo(
                "Dry run.  Nothing is looked up and nothing is sent.  "
                "Pass --write to place them."
            )
            for address in unknown[:20]:
                click.echo(f"    would look up  {address}")
            return
        if not addresses:
            click.echo("Nothing to place.")
            return

        look = lookup_for(geocoder, **({"api_key": api_key} if api_key else {}))
        answers = placed(addresses, look, cache)
        click.echo(f"saved {cache.save()}")

        sent = refused = blank = coarse = 0
        for key, (trip_id, gaps) in work.items():
            named = staged_trip(archive, key)
            click.echo(f"\n{named.name if named else key}")
            for gap in gaps:
                found = answers[gap.address]
                if not found.placed or found.position is None:
                    blank += 1
                    click.echo(f"    no answer   {gap.where}: {gap.address}")
                    continue
                if not plausible(found.position, references[key], far_km):
                    away = far_from(found.position, references[key])
                    refused += 1
                    click.echo(
                        f"    REFUSED     {gap.where}: {gap.address} -> "
                        f"{found.label} ({away:.0f} km away)"
                    )
                    continue
                lat, lon = found.position
                client.update_child(
                    trip_id,
                    gap.collection,
                    gap.child_id,
                    {
                        f"{gap.prefix}latitude": lat,
                        f"{gap.prefix}longitude": lon,
                    },
                )
                sent += 1
                # The label is what makes a wrong answer visible.  'Elko,
                # NV' resolves to Elko County, 54 km from the town, and no
                # distance check against the rest of a trip will see that.
                #
                mark = "COARSE  " if found.coarse else "placed  "
                if found.coarse:
                    coarse += 1
                click.echo(f"    {mark}    {gap.where}  {lat:.4f},{lon:.4f}")
                click.echo(f"                    {found.label}")

    click.echo(
        f"\n{sent} endpoints placed, {refused} refused as implausible, "
        f"{blank} found nowhere"
    )
    if coarse:
        click.echo(
            f"{coarse} matched something larger than the place asked for "
            "-- a county rather than its town, say.  Check the labels "
            "above, then --forget the address and run again."
        )
    click.echo(
        f"{cache.hits} answered from the cache, {cache.misses} asked of "
        f"{geocoder}"
    )


if __name__ == "__main__":
    main()
