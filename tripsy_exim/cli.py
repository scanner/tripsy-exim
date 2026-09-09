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

# 3rd party imports
import click

from tripsy_exim import __version__


########################################################################
#
@click.group()
@click.version_option(version=__version__, prog_name="tripsy-exim")
def main() -> None:
    """Export and import trip data for Tripsy.app."""


if __name__ == "__main__":
    main()
