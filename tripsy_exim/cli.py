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
    in_travel_order,
    plan_trip,
    resolve_trip_key,
    staged_trip,
    upload_trip,
    uploaded_trips,
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
        click.echo(f"  {mark}  {str(starts or ''):10}  {name[:48]:48} {key}")

    click.echo(f"\n{shown} trips, {len(done)} already uploaded")


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

    # Two staged trips sharing a name and a date range are either one
    # journey recorded twice or one journey recorded per traveller.  A
    # per-trip plan cannot show it, and uploading both makes two rival
    # trips out of what the app should hold as one.
    #
    planned = {plan.trip_key for plan in plans}
    index = archive.read_manifest().get(TRIP_INDEX) or {}
    for join_key, shared_keys in sorted(index.items()):
        shared = [key for key in shared_keys if key in planned]
        if len(shared) > 1:
            click.echo(f"\n  NOTE: {len(shared)} trips share one key:")
            click.echo(f"      {join_key}")
            for key in shared:
                click.echo(f"      {key}")

    if not write:
        click.echo("\nDry run.  Nothing was sent.  Pass --write to upload.")
        return

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


if __name__ == "__main__":
    main()
