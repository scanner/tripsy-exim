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
import subprocess
from collections import Counter
from pathlib import Path

# 3rd party imports
import click
from dotenv import load_dotenv

# Project imports
from tripsy_exim import __version__
from tripsy_exim.api import IMPORT, INTERACTIVE, TokenAuth, TripsyClient
from tripsy_exim.models import scratch_namespace
from tripsy_exim.store import Archive
from tripsy_exim.sync import TRIP_INDEX, stage_export_file, stage_file
from tripsy_exim.sync.importer import import_trip, plan_trip


########################################################################
#
@click.group()
@click.version_option(version=__version__, prog_name="tripsy-exim")
def main() -> None:
    """Export and import trip data for Tripsy.app."""


########################################################################
########################################################################
#
# Credentials
#
# Resolved here and nowhere else.  Nothing below this module reads the
# environment, runs `op`, or holds a password: the token from POST /auth
# is handed to the client and lives in memory for the run.
#


####################################################################
#
def op_read(url: str, field: str) -> str:
    """
    Read one field of a 1Password item.

    The binary is named rather than found, because more than one `op` can
    be on a PATH and only the one the desktop app authorised can reach an
    account.  `TRIPSY_OP_BIN` points at it when the first on the PATH is
    the wrong one.

    Args:
        url: An `op://vault/item` URL, without a field.
        field: The field to read.

    Returns:
        The field's value.

    Raises:
        click.ClickException: `op` could not answer, with its own words.
    """
    binary = os.environ.get("TRIPSY_OP_BIN", "op")
    result = subprocess.run(
        [binary, "read", f"{url.rstrip('/')}/{field}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"{binary} read {url}/{field} failed: "
            f"{result.stderr.strip() or 'no output'}"
        )
    return result.stdout.strip()


####################################################################
#
def resolve_credentials(
    username: str | None, password: str | None
) -> tuple[str, str]:
    """
    Settle which credentials a run authenticates with.

    First match wins: a command line flag, then the environment, then
    `.env`, then 1Password.  The 1Password form keeps a plaintext
    password out of the environment entirely, which is what a scheduled
    run wants.

    Args:
        username: Username given on the command line, or None.
        password: Password given on the command line, or None.

    Returns:
        The username and password to authenticate with.

    Raises:
        click.ClickException: Nothing resolved to a usable pair.
    """
    load_dotenv()

    username = username or os.environ.get("TRIPSY_USERNAME")
    password = password or os.environ.get("TRIPSY_PASSWORD")
    if username and password:
        return username, password

    url = os.environ.get("TRIPSY_ONEPASSWORD_URL")
    if url:
        return (
            username or op_read(url, "username"),
            password or op_read(url, "password"),
        )

    raise click.ClickException(
        "no credentials: pass --username/--password, set "
        "TRIPSY_USERNAME and TRIPSY_PASSWORD, or point "
        "TRIPSY_ONEPASSWORD_URL at an op:// item"
    )


####################################################################
#
def authenticate(username: str | None, password: str | None) -> str:
    """
    Trade resolved credentials for an API token.

    Args:
        username: Username given on the command line, or None.
        password: Password given on the command line, or None.

    Returns:
        The token, which the caller passes to its own client.
    """
    name, secret = resolve_credentials(username, password)
    with TripsyClient(profile=INTERACTIVE) as client:
        return client.login(name, secret)


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
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory the canonical objects are written to.",
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
    archive_root: Path,
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
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory the canonical objects are written to.",
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
    archive_root: Path,
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
@main.command("import")
@click.option(
    "--archive",
    "archive_root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory the staged trips are read from.",
)
@click.option(
    "--trip",
    "trip_keys",
    multiple=True,
    help="Import only this trip key.  Repeatable; default is every trip.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Import at most this many trips, for a cautious first run.",
)
@click.option(
    "--write/--dry-run",
    default=False,
    help=(
        "Actually send to Tripsy.  The default plans and prints without "
        "writing, since an identifier Tripsy has seen is never released."
    ),
)
@click.option("--username", default=None, help="Tripsy account username.")
@click.option("--password", default=None, help="Tripsy account password.")
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="List every object a trip would write, not just the totals.",
)
def import_command(
    archive_root: Path,
    trip_keys: tuple[str, ...],
    limit: int | None,
    write: bool,
    username: str | None,
    password: str | None,
    verbose: bool,
) -> None:
    """
    Write staged trips to Tripsy.

    Plans first and prints the plan.  Without `--write` that is all it
    does: nothing is sent, no credentials are needed, and the numbers
    shown are the ones a real run would send.

    Re-running is a no-op rather than a source of duplicates, so a run
    that failed part way is resumed by running it again.
    """
    archive = Archive(archive_root)
    keys = list(trip_keys) or archive.trip_keys()
    if not keys:
        raise click.ClickException(f"no staged trips in {archive_root}")
    if limit is not None:
        keys = keys[:limit]

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

    # Two staged trips sharing a name and a date range are either one
    # journey recorded twice or one journey recorded per traveller.  A
    # per-trip plan cannot show it, and importing both makes two rival
    # trips out of what the app should hold as one.
    #
    planned = {plan.trip_key for plan in plans}
    index = archive.read_manifest().get(TRIP_INDEX) or {}
    for join_key, keys in sorted(index.items()):
        shared = [key for key in keys if key in planned]
        if len(shared) > 1:
            click.echo(f"\n  NOTE: {len(shared)} trips share one key:")
            click.echo(f"      {join_key}")
            for key in shared:
                click.echo(f"      {key}")

    if not write:
        click.echo("\nDry run.  Nothing was sent.  Pass --write to import.")
        return

    token = authenticate(username, password)
    created = existing = 0
    failures: list[str] = []
    with TripsyClient(profile=IMPORT, auth=TokenAuth(token)) as client:
        for plan in plans:
            result = import_trip(client, archive, plan.trip_key)
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
