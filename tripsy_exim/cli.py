#!/usr/bin/env python
#
"""
Command line entry point for tripsy-exim.

Subcommands are added by the import, export, and status work; this module
owns only the command group and the options common to every subcommand.

This is also the single place credentials are resolved -- see
`open_session` for the token and `resolve_credentials` for the username
and password.  Nothing else in the package reads the environment or
holds a password.
"""

# system imports
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

# 3rd party imports
import click
from click.decorators import FC
from dotenv import find_dotenv, load_dotenv

# Project imports
from tripsy_exim import __version__
from tripsy_exim.api import (
    BACKUP,
    IMPORT,
    INTERACTIVE,
    PacingProfile,
    TokenAuth,
    TripsyClient,
)
from tripsy_exim.api.errors import (
    AuthenticationError,
    BadRequest,
    TripsyError,
)
from tripsy_exim.geocode import (
    Cache,
    far_from,
    lookup_for,
    normalised,
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
from tripsy_exim.store import (
    ARCHIVE_ENV,
    DEFAULT_ARCHIVE,
    STAGED_DIR,
    Archive,
    default_root,
    exports_path,
    staged_path,
)
from tripsy_exim.sync import TRIP_INDEX, stage_export_file, stage_file
from tripsy_exim.sync.backfill import (
    POPULATIONS,
    Gap,
    by_population,
    by_recurrence,
    gaps,
)
from tripsy_exim.sync.exporter import (
    Progress,
    Selection,
    TripWritten,
    default_fetch,
    export,
    only_one_run,
)
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
from tripsy_exim.sync.infer import (
    DISAGREE_KM,
    filled_rows,
    inferences,
)
from tripsy_exim.sync.worklist import (
    WorkListError,
    apply_rows,
    draft_rows,
    read_worklist,
    write_worklist,
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
def can_prompt() -> bool:
    """Whether someone is at a terminal to be asked."""
    return sys.stdin.isatty()


####################################################################
#
def resolve_credentials(
    username: str | None, password: str | None, store: SecretStore | None
) -> tuple[str, str]:
    """
    Find the username and password to log in with.

    Each is taken, separately, from the first place that has it:

      1. the --username / --password flag
      2. TRIPSY_USERNAME / TRIPSY_PASSWORD, from the environment or .env
      3. the secret store named by TRIPSY_SECRET_URL
      4. a prompt, when running at a terminal

    Args:
        username: Username given on the command line, or None.
        password: Password given on the command line, or None.
        store: The configured secret store, or None.

    Returns:
        The username and password.

    Raises:
        click.ClickException: A value is still missing after all four.
    """
    username = username or os.environ.get("TRIPSY_USERNAME")
    password = password or os.environ.get("TRIPSY_PASSWORD")

    if store is not None:
        try:
            username = username or store.get(USERNAME)
            password = password or store.get(PASSWORD)
        except SecretError as exc:
            raise click.ClickException(str(exc)) from exc

    if can_prompt():
        username = username or click.prompt("Tripsy username", err=True)
        password = password or click.prompt(
            "Tripsy password", err=True, hide_input=True
        )

    if username and password:
        return username, password

    raise click.ClickException(
        "no Tripsy username and password: pass --username/--password, set "
        "TRIPSY_USERNAME and TRIPSY_PASSWORD, put them in the store named "
        f"by {SECRET_URL_ENV}, or run at a terminal to be asked for them"
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
    Yield a client authenticated with a Tripsy API token.

    Where the token comes from:

      1. If the store named by TRIPSY_SECRET_URL holds one, it is used.
      2. Otherwise the run logs in with a username and password -- see
         `resolve_credentials` -- and, when a store is configured, saves
         the new token there.
      3. If Tripsy refuses a stored token, the run logs in once more, as
         in 2, and the new token replaces it.

    Args:
        username: Username given on the command line, or None.
        password: Password given on the command line, or None.
        profile: Pacing profile for the run.

    Yields:
        A client carrying a token.
    """
    try:
        store = store_for()
    except SecretError as exc:
        raise click.ClickException(str(exc)) from exc

    ####################################################################
    #
    def log_in() -> str:
        """Trade a username and password for a token, and save it."""
        name, secret = resolve_credentials(username, password, store)
        try:
            with TripsyClient(profile=INTERACTIVE) as session:
                token = session.login(name, secret)
        except (BadRequest, AuthenticationError) as exc:
            raise click.ClickException(
                f"Tripsy refused the username and password for {name}"
            ) from exc

        if store is None:
            click.echo(
                f"logged in to Tripsy as {name}; the token is not saved "
                f"because {SECRET_URL_ENV} is not set",
                err=True,
            )
            return token

        try:
            store.put(TOKEN, token)
        except SecretError as exc:
            # Not fatal: this run has its token, and the next one will
            # log in again.
            #
            click.echo(
                f"logged in to Tripsy as {name}; could not save the token "
                f"to {store.url}: {exc}",
                err=True,
            )
        else:
            click.echo(
                f"logged in to Tripsy as {name}; token saved to {store.url}",
                err=True,
            )
        return token

    stored = None
    if store is not None:
        try:
            stored = store.get(TOKEN)
        except SecretError as exc:
            raise click.ClickException(str(exc)) from exc

    client = TripsyClient(
        profile=profile,
        auth=TokenAuth(stored or log_in()),
        reauthenticate=log_in,
    )
    try:
        yield client
    finally:
        client.close()


####################################################################
#
def plural(count: int, singular: str, suffix: str = "s") -> str:
    """
    A count and its noun, agreeing with each other.

    Summary lines are read far more often than they are written, and a
    run that reports '1 trips' reads as a bug in the counting.

    Args:
        count: How many.
        singular: What one of them is called.
        suffix: What to add for more than one.

    Returns:
        The count and the noun, e.g. '1 trip' or '2 trips'.
    """
    return f"{count} {singular}{'' if count == 1 else suffix}"


####################################################################
#
def archive_options(purpose: str) -> Callable[[FC], FC]:
    """
    Attach `--archive` and `--archive-root` to a command.

    Every command settles which archive it works on the same way, so the
    pair is declared once.  `--archive` names a staging archive; only
    `--archive-root` takes a path, and most runs never need it.

    Args:
        purpose: What this command does with the archive.  It becomes
            the first sentence of `--archive`'s help, so it reads as
            that command's own.

    Returns:
        A decorator adding both options to a command.
    """

    ####################################################################
    #
    def attach(command: FC) -> FC:
        """Wrap one command in both options."""
        command = click.option(
            "--archive-root",
            "archive_root",
            default=None,
            type=click.Path(file_okay=False, path_type=Path),
            help=(
                "Directory holding the staging archives and the exports.  "
                f"Defaults to ${ARCHIVE_ENV}, or "
                "~/.local/share/tripsy-exim."
            ),
        )(command)
        return click.option(
            "--archive",
            "archive_name",
            default=DEFAULT_ARCHIVE,
            show_default=True,
            help=f"{purpose}  Named under <root>/{STAGED_DIR}/.",
        )(command)

    return attach


####################################################################
#
def archive_for(archive_name: str, archive_root: Path | None) -> Path:
    """
    Settle which directory one named staging archive is.

    A flag wins, then `TRIPSY_EXIM_ARCHIVE`, then the XDG data
    directory.  A leading `~` is expanded wherever the value came from:
    a shell expands one on the command line, but nothing expands one
    written in `.env` or exported with quotes.

    Args:
        archive_name: The archive's name, as given on the command line.
        archive_root: The root named on the command line, or None.

    Returns:
        The directory to read and write.

    Raises:
        click.ClickException: The name has no place in a path.
    """
    root = (
        Path(archive_root).expanduser()
        if archive_root is not None
        else default_root()
    )
    try:
        return staged_path(root, archive_name)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


####################################################################
#
def staged_archive(archive_name: str, archive_root: Path | None) -> Archive:
    """
    Open an archive that is expected to already hold trips.

    Args:
        archive_name: The archive's name, as given on the command line.
        archive_root: The root named on the command line, or None.

    Returns:
        The archive.

    Raises:
        click.ClickException: There is no such directory.  The resolved
            path is named, since it may have come from `.env` or a
            default rather than from the command line.
    """
    root = archive_for(archive_name, archive_root)
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
@archive_options("Staging archive the canonical objects are written to.")
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
    archive_name: str,
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

    archive = Archive(archive_for(archive_name, archive_root))
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
        f"\n{len(sources)} file{plural}, {total} objects into {archive.root}"
    )


####################################################################
#
@main.command("stage-export")
@click.argument(
    "export",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@archive_options("Staging archive the canonical objects are written to.")
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
    archive_name: str,
    archive_root: Path | None,
    namespace: str | None,
    scratch: bool,
) -> None:
    """
    Parse a TripIt JSON export into the local archive.

    Nothing is sent to Tripsy.  One export carries a whole account, so
    every trip in it is staged in one pass, each with the reader's report
    beside it, ready to be reviewed and corrected before anything is
    posted.

    The export is authoritative for a trip's identity, so stage it before
    any calendars: a `.ics` naming a trip already staged from here is
    refused rather than staged a second time.
    """
    namespace = resolve_namespace(namespace, scratch)

    archive = Archive(archive_for(archive_name, archive_root))
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
        f"\n{len(staged)} trip{plural}, {objects} objects into {archive.root}"
    )
    if summary:
        click.echo(f"  {summary}")


####################################################################
#
@main.command("list")
@archive_options("Staging archive the trips are read from.")
@click.option(
    "--pending/--all",
    default=False,
    help="List only trips no run has finished uploading.",
)
def list_command(
    archive_name: str, archive_root: Path | None, pending: bool
) -> None:
    """
    List the trips staged in the archive, oldest first.

    The mark in the first column says whether a run has finished
    uploading that trip.
    """
    archive = staged_archive(archive_name, archive_root)
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

    click.echo(f"\n{plural(shown, 'trip')}, {len(done)} already uploaded")


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
@archive_options("Staging archive the trips are read from.")
def merge_command(
    absorbed: str,
    target: str | None,
    archive_name: str,
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
    archive = staged_archive(archive_name, archive_root)

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
@archive_options("Staging archive the trips are read from.")
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
    archive_name: str,
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
    archive = staged_archive(archive_name, archive_root)
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
        f"\n{plural(len(plans), 'trip')}, {plural(objects, 'object')}, "
        f"{untyped} untyped legs"
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
@archive_options("Staging archive the trips are read from.")
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
    archive_name: str,
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

    It also lists objects carrying an address and no position.  Nothing
    on the server resolves those: the app geocodes an activity's address
    when it renders it, and a transportation endpoint is never geocoded
    at all.  So an unplaced activity may place itself once the trip is
    opened, and an unplaced leg will not.  `fix-locations` is what places
    the rest.
    """
    archive = staged_archive(archive_name, archive_root)

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
            f"{unplaced} addresses carry no position.  An activity may "
            "place itself once the app renders the trip; a leg never "
            "will.  `fix-locations` places the rest."
        )


####################################################################
#
@main.command("fix-locations")
@archive_options("Staging archive --trip is resolved against.")
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
    help=(
        "Actually send the positions to Tripsy.  A dry run asks the "
        "geocoder nothing and sends nothing; it reports what the cache "
        "already answers and what would have to be looked up."
    ),
)
@click.option("--username", default=None, help="Tripsy account username.")
@click.option("--password", default=None, help="Tripsy account password.")
def fix_locations_command(
    archive_name: str,
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
    archive = staged_archive(archive_name, archive_root)

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
        # Dropped from the cache in memory either way, so a dry run can
        # report what forgetting leads to.  Only a write makes it
        # durable: a cache that outlives the run is what Nominatim's
        # terms rest on, and a plan does not get to shorten it.
        #
        # Keyed by the normalised form, which is how a lookup stored it.
        #
        dropped = cache.forget(normalised(address))
        if not dropped:
            state = "was not held"
        else:
            state = "forgotten" if write else "would be forgotten"
        click.echo(f"  {address}: {state}")
    if forget and write:
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
        if not addresses:
            click.echo("Nothing to place.")
            return

        # A dry run asks nobody anything.  A lookup is a request to an
        # outside service under a policy that counts them, so it is an
        # outward act of its own and not something a plan should do.  What
        # the cache already holds is answered from here, which is what
        # makes the run a preview rather than a list of addresses.
        #
        if write:
            look = lookup_for(
                geocoder, **({"api_key": api_key} if api_key else {})
            )
            answers = placed(addresses, look, cache)
            click.echo(f"saved {cache.save()}")
        else:
            click.echo(
                "Dry run.  Nothing is looked up and nothing is sent.  "
                "Pass --write to place them."
            )
            answers = {}
            for address in set(addresses):
                held = cache.get(normalised(address))
                if held is not None:
                    answers[address] = held

        sent = refused = blank = coarse = pending = 0
        for key, (trip_id, gaps) in work.items():
            named = staged_trip(archive, key)
            click.echo(f"\n{named.name if named else key}")
            for gap in gaps:
                found = answers.get(gap.address)
                if found is None:
                    pending += 1
                    click.echo(f"    would look up  {gap.where}: {gap.address}")
                    continue
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
                if write:
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
                placing = "placed  " if write else "would place"
                mark = "COARSE  " if found.coarse else placing
                if found.coarse:
                    coarse += 1
                click.echo(f"    {mark}    {gap.where}  {lat:.4f},{lon:.4f}")
                click.echo(f"                    {found.label}")

    click.echo(
        f"\n{sent} endpoints {'placed' if write else 'would be placed'}, "
        f"{refused} refused as implausible, {blank} found nowhere"
    )
    if pending:
        click.echo(f"{pending} endpoints need a lookup; pass --write")
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


####################################################################
#
@main.group("backfill")
def backfill_group() -> None:
    """
    Author corrections against what the archive already knows.

    Staging records where the parser was unsure and moves on, because a
    parse that stopped to ask would never finish.  These commands read
    those doubts back out, together with the objects the app would have
    no way to place, so corrections are written against a list rather
    than against a directory of JSON files.
    """


####################################################################
#
@backfill_group.command("report")
@archive_options("Staging archive the trips are read from.")
@click.option(
    "--population",
    "wanted_populations",
    multiple=True,
    type=click.Choice(POPULATIONS),
    help="Report only these kinds of gap.  Repeatable; default is all.",
)
@click.argument("wanted", nargs=-1)
def backfill_report_command(
    archive_name: str,
    archive_root: Path | None,
    wanted_populations: tuple[str, ...],
    wanted: tuple[str, ...],
) -> None:
    """
    Say what the staged trips still need a person for.

    Nothing is written.  Trips are named by key or by part of a name;
    naming none reports the whole archive.

    Gaps are counted against each trip as that trip would upload, with
    any corrections already authored laid over, so answering one and
    running again reports one fewer rather than the same list.

    The places are listed commonest first, because that is the order
    that finishes soonest: one airport or station recurs across a whole
    corpus, and answering it once closes every gap that names it.
    """
    archive = staged_archive(archive_name, archive_root)

    if wanted:
        try:
            keys = [resolve_trip_key(archive, needle) for needle in wanted]
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        keys = archive.trip_keys()
    if not keys:
        raise click.ClickException(f"no staged trips in {archive.root}")

    asked = set(wanted_populations) or set(POPULATIONS)
    found: list[Gap] = []
    for key in in_travel_order(archive, keys):
        rows = [g for g in gaps(archive, key) if g.population in asked]
        if not rows:
            continue
        found.extend(rows)

        trip = staged_trip(archive, key)
        click.echo(f"\n{str(getattr(trip, 'name', '') or key)}")
        for population, in_population in by_population(rows).items():
            click.echo(
                f"  {population:18} {len(in_population):4}  "
                f"{in_population[0].remedy}"
            )

    if not found:
        click.echo(f"{plural(len(keys), 'trip')}, nothing open")
        return

    click.echo("\nby place, commonest first:")
    for label, rows in by_recurrence(found):
        endpoints = ", ".join(
            sorted({row.endpoint for row in rows if row.endpoint})
        )
        click.echo(
            f"  {len(rows):4}x  {label[:44]:44} "
            f"{endpoints or rows[0].population}"
        )

    click.echo(
        f"\n{plural(len(found), 'gap')} across {plural(len(keys), 'trip')}"
    )


####################################################################
#
@backfill_group.command("draft")
@click.argument("destination", type=click.Path(dir_okay=False, path_type=Path))
@archive_options("Staging archive the trips are read from.")
@click.option(
    "--population",
    "wanted_populations",
    multiple=True,
    type=click.Choice(POPULATIONS),
    help="Write rows for only these kinds of gap.  Repeatable.",
)
@click.argument("wanted", nargs=-1)
def backfill_draft_command(
    destination: Path,
    archive_name: str,
    archive_root: Path | None,
    wanted_populations: tuple[str, ...],
    wanted: tuple[str, ...],
) -> None:
    """
    Write the work-list to DESTINATION, to be edited and applied back.

    Each row arrives pre-filled with what the object holds now: empty
    for an endpoint nothing places, the parser's guess for a timezone it
    was unsure of.  Fill a row in and `backfill apply` writes it as a
    correction; leave it alone and nothing happens.

    A row whose value is an empty string is one deliberately left blank
    and is skipped, as is any key ending in '_note', which is there for
    a reader rather than for the archive.
    """
    archive = staged_archive(archive_name, archive_root)

    if wanted:
        try:
            keys = [resolve_trip_key(archive, needle) for needle in wanted]
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        keys = archive.trip_keys()
    if not keys:
        raise click.ClickException(f"no staged trips in {archive.root}")

    asked = set(wanted_populations) or set(POPULATIONS)
    rows = [
        row
        for row in draft_rows(archive, in_travel_order(archive, keys))
        if row.get("population") in asked
    ]
    if not rows:
        raise click.ClickException("nothing is open; no work-list written")

    write_worklist(destination, archive, rows)
    click.echo(f"{len(rows)} rows written to {destination}")
    click.echo(
        f"Edit it, then: tripsy-exim backfill apply {destination} --write"
    )


####################################################################
#
@backfill_group.command("apply")
@click.argument(
    "source", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@archive_options("Staging archive the trips are read from.")
@click.option(
    "--write/--dry-run",
    default=False,
    help="Save the corrections.  Without it, report what would be saved.",
)
def backfill_apply_command(
    source: Path,
    archive_name: str,
    archive_root: Path | None,
    write: bool,
) -> None:
    """
    Read an edited work-list back and write it as corrections.

    Dry run by default.  Nothing is saved until --write, and a run that
    changes nothing says so rather than rewriting the correction files.

    Only what differs from what the object already holds is written, so
    running the same file twice does nothing the second time.  Existing
    corrections are merged rather than replaced: applying a second
    work-list does not discard the first.
    """
    archive = staged_archive(archive_name, archive_root)

    try:
        rows = read_worklist(source, archive)
    except WorkListError as exc:
        raise click.ClickException(str(exc)) from exc

    outcome = apply_rows(archive, rows, write=write)

    for label, why in outcome.refused:
        click.echo(f"  refused  {label}: {why}")

    verb = "written" if write else "would be written"
    click.echo(
        f"\n{outcome.written} {verb} -- {outcome.corrected} corrected, "
        f"{outcome.retyped} retyped, {outcome.added} added"
    )
    click.echo(
        f"{outcome.unchanged} already correct, "
        f"{outcome.left_blank} left blank, {len(outcome.refused)} refused"
    )
    if outcome.written and not write:
        click.echo("\nNothing was saved.  Pass --write to save it.")


####################################################################
#
@backfill_group.command("infer")
@archive_options("Staging archive the trips are read from.")
@click.option(
    "--disagree-km",
    default=DISAGREE_KM,
    show_default=True,
    type=float,
    help=(
        "Refuse a code whose recorded positions sit further apart than "
        "this, on the grounds that it names two places rather than one."
    ),
)
@click.option(
    "--write/--dry-run",
    default=False,
    help="Save the corrections.  Without it, report what would be saved.",
)
@click.argument("wanted", nargs=-1)
def backfill_infer_command(
    archive_name: str,
    archive_root: Path | None,
    disagree_km: float,
    write: bool,
    wanted: tuple[str, ...],
) -> None:
    """
    Place what the archive can work out from itself.

    An export places nearly everything it carries, so an endpoint naming
    an airport code without a position usually names one that some other
    segment placed.  This finds those and fills them in, with no human
    input and nothing asked of any outside service.

    Only exact keys are matched.  An airport code is three letters and
    nothing else; a station gives its name instead, and free text is not
    a key -- two spellings of one station are two keys, and one spelling
    can be two stations.

    Dry run by default.  Run this before `backfill report`, so what is
    left for a person to answer is only what the archive could not.
    """
    archive = staged_archive(archive_name, archive_root)

    if wanted:
        try:
            keys = [resolve_trip_key(archive, needle) for needle in wanted]
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        keys = archive.trip_keys()
    if not keys:
        raise click.ClickException(f"no staged trips in {archive.root}")

    # Positions are gathered across the whole archive even when one trip
    # is named: the airport this trip left unplaced was very likely
    # placed on another.
    #
    found = inferences(archive, in_travel_order(archive, keys), disagree_km)
    if not found:
        click.echo("nothing carries a code this could work from")
        return

    answered = [i for i in found if i.answered]
    for inference in answered:
        assert inference.position is not None
        latitude, longitude = inference.position
        click.echo(
            f"  {inference.key}  {latitude:9.4f},{longitude:10.4f}  "
            f"{inference.agreed} agreed   {inference.gap.trip_key[:16]}"
        )

    for inference in found:
        if inference.refusal:
            click.echo(f"  refused  {inference.gap.where}: {inference.refusal}")

    outcome = apply_rows(archive, filled_rows(archive, found), write=write)

    verb = "placed" if write else "would be placed"
    click.echo(
        f"\n{outcome.written} {verb}, {len(found) - len(answered)} refused"
    )
    if outcome.refused:
        for label, why in outcome.refused:
            click.echo(f"  refused  {label}: {why}")
    if outcome.written and not write:
        click.echo("\nNothing was saved.  Pass --write to save it.")


####################################################################
#
def report_trip(total: int) -> Progress:
    """
    Build a progress report that names each trip as it is written.

    Args:
        total: How many trips the run was asked for.

    Returns:
        A callback for `export`, printing one line per trip.
    """
    done = 0

    ####################################################################
    #
    def report(written: TripWritten) -> None:
        """Print one line for the trip just written."""
        nonlocal done
        done += 1
        name = written.payload.get("name") or written.payload.get("id")
        click.echo(
            f"  [{done}/{total}] {name}: "
            f"{plural(written.objects, 'object')}, "
            f"{plural(written.documents, 'document')}"
        )

    return report


####################################################################
#
@main.command("export")
@click.option(
    "--archive-root",
    "archive_root",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help=(
        "Directory holding the staging archives and the exports.  "
        f"Defaults to ${ARCHIVE_ENV}, or ~/.local/share/tripsy-exim."
    ),
)
@click.option(
    "--all",
    "everything",
    is_flag=True,
    default=False,
    help="Export every trip the account holds.",
)
@click.option(
    "--trip",
    "wanted",
    multiple=True,
    help=(
        "Export this trip, by Tripsy id or by part of its name.  Repeatable."
    ),
)
@click.option(
    "--glob",
    default=None,
    help="Export trips whose name matches this pattern, e.g. 'Japan*'.",
)
@click.option(
    "--from",
    "since",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Export trips that were still running on or after this day.",
)
@click.option(
    "--to",
    "until",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Export trips that had started on or before this day.",
)
@click.option("--username", default=None, help="Tripsy account username.")
@click.option("--password", default=None, help="Tripsy account password.")
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="Name each trip as it is written.  Quiet by default, for cron.",
)
def export_command(
    archive_root: Path | None,
    everything: bool,
    wanted: tuple[str, ...],
    glob: str | None,
    since: datetime | None,
    until: datetime | None,
    username: str | None,
    password: str | None,
    verbose: bool,
) -> None:
    """
    Write a dated export of what Tripsy holds.

    Each run writes its own directory under the archive root, named for
    the instant it started, holding one directory per trip.  Nothing is
    compared against an earlier run and nothing is pruned: a backup is
    made, not reconciled.

    Say what to take.  `--all` is every trip; `--trip` and `--glob` name
    them; `--from` and `--to` narrow whatever was named to trips that
    were under way in that window.  Asking for nothing is refused.

    Quiet unless something is written, so a scheduled run that changes
    nothing sends no mail.  Exits 2 when another run holds the lock,
    which is a thing to skip rather than a failure to report.
    """
    try:
        selection = Selection(
            everything=everything,
            trips=tuple(wanted),
            glob=glob,
            since=since.date() if since else None,
            until=until.date() if until else None,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    root = (
        Path(archive_root).expanduser()
        if archive_root is not None
        else default_root()
    )
    destination = exports_path(root)

    try:
        with only_one_run(destination):
            with open_session(username, password, profile=BACKUP) as client:
                pacer = client.pacer
                chosen = selection.select(client.iter_trips())
                if not chosen:
                    raise click.ClickException(
                        "nothing matched; no export written"
                    )
                if verbose:
                    click.echo(f"exporting {plural(len(chosen), 'trip')}")
                outcome = export(
                    client,
                    destination,
                    chosen,
                    scope=selection.scope,
                    fetch=default_fetch,
                    progress=report_trip(len(chosen)) if verbose else None,
                )
    except BlockingIOError as exc:
        # Not a failure: a run that overlapped another has nothing to do
        # and nothing to report.  A scheduler is told apart from a real
        # error by the code, so a nightly job does not page anybody for
        # having started while yesterday's was still going.
        #
        click.echo(str(exc), err=True)
        raise SystemExit(2) from exc
    except (OSError, TripsyError) as exc:
        raise click.ClickException(f"export failed: {exc}") from exc

    click.echo(
        f"{plural(outcome.trips, 'trip')}, "
        f"{plural(outcome.objects, 'object')}, "
        f"{plural(outcome.documents, 'document')} into {outcome.path}"
    )
    if outcome.quarantined:
        click.echo(
            f"{plural(len(outcome.quarantined), 'payload')} quarantined",
            err=True,
        )
    if verbose:
        click.echo(
            f"paced as {pacer.profile.name}: "
            f"{plural(pacer.requests, 'request')}, "
            f"{pacer.throttles} throttled, {pacer.failures} failed, "
            f"{pacer.total_wait:.1f}s spent waiting"
        )


if __name__ == "__main__":
    main()
