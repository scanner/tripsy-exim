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
from collections import Counter
from pathlib import Path

# 3rd party imports
import click

# Project imports
from tripsy_exim import __version__
from tripsy_exim.models import scratch_namespace
from tripsy_exim.store import Archive
from tripsy_exim.sync import stage_export_file, stage_file


########################################################################
#
@click.group()
@click.version_option(version=__version__, prog_name="tripsy-exim")
def main() -> None:
    """Export and import trip data for Tripsy.app."""


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


if __name__ == "__main__":
    main()
