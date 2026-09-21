#!/usr/bin/env python
#
"""
Fixtures shared by the command line tests.

Every command is driven through `CliRunner`, and every command but the
staging ones needs an archive to work on rather than a source file.
Building one is groundwork rather than part of any test, so it arrives
as a fixture.

Test input arrives as a fixture throughout, including the trip pairs:
what a test consumes should be readable from its signature.
"""

# system imports
import json
from collections.abc import Callable
from pathlib import Path

# 3rd party imports
import pytest
from click.testing import CliRunner

# Project imports
from tests import tripit_builder as b
from tripsy_exim.cli import main
from tripsy_exim.store import DEFAULT_ARCHIVE, Archive, staged_path

# Two trips whose names share a word and differ in another.  Suffixed
# because a bare place name elsewhere in the suite is a position.
# Both halves are load-bearing: 'kyoto' has to pick one trip and 'japan' has to pick
# both, which is what makes one of them a match and the other ambiguous.
#
OSAKA_TRIP = "Osaka, Japan, May 2024"
KYOTO_TRIP = "Kyoto, Japan, June 2024"

# Two trips eight months apart.  The names say which is which, so a test
# reads its own output without working the dates out.
#
EARLIER = "Earlier trip"
LATER = "Later trip"


####################################################################
#
@pytest.fixture
def write_export(tmp_path: Path) -> Callable[..., Path]:
    """Write a synthetic JSON export to disk and give back its path."""

    def write(*trips: dict) -> Path:
        """One synthetic JSON export on disk."""
        path = tmp_path / "export.json"
        path.write_text(json.dumps(b.export(*trips)))
        return path

    return write


####################################################################
#
@pytest.fixture
def named_pair() -> tuple[dict, dict]:
    """Two trips sharing a word in their names."""
    return (
        b.trip(name=OSAKA_TRIP, objects=[b.flight()]),
        b.trip(
            name=KYOTO_TRIP,
            start="2024-06-01",
            end="2024-06-04",
            objects=[b.lodging()],
        ),
    )


####################################################################
#
@pytest.fixture
def dated_pair() -> tuple[dict, dict]:
    """
    Two trips far apart in time, given later first.

    Always the wrong way round, so a test that gets them back in travel
    order has shown it ordered them rather than kept them as they came.
    """
    return (
        b.trip(
            name=LATER,
            start="2024-09-01",
            end="2024-09-04",
            objects=[b.flight()],
        ),
        b.trip(
            name=EARLIER,
            start="2024-01-01",
            end="2024-01-04",
            objects=[b.lodging()],
        ),
    )


####################################################################
#
@pytest.fixture
def staged(
    runner: CliRunner, tmp_path: Path, write_export: Callable[..., Path]
) -> Callable[..., Path]:
    """
    Stage trips into a fresh archive and give back the archive root.

    Every command but the staging ones needs an archive to work on
    rather than an export, so building one is groundwork rather than
    part of any test.

    What comes back is the root, which is what a command is given.  The
    archive itself is one level down, under `staged/`; `opened` is how a
    test reads it back.

    `archive` names which one to stage into, so calling this twice under
    one root gives a test two archives to tell apart.

    The root is deliberately not `tmp_path / "archive"`: that is where
    the `archive` fixture in the parent conftest puts an Archive, and a
    test taking both would otherwise get one rooted at the root, reading
    nothing and saying nothing about why.
    """

    def stage(*trips: dict, archive: str = DEFAULT_ARCHIVE) -> Path:
        """Stage these trips, or one ordinary one when none are named."""
        if not trips:
            trips = (b.trip(objects=[b.flight()]),)
        export = write_export(*trips)
        archive_root = tmp_path / "archives"
        result = runner.invoke(
            main,
            [
                "stage-export",
                str(export),
                "--archive-root",
                str(archive_root),
                "--archive",
                archive,
            ],
        )
        assert result.exit_code == 0, result.output
        return archive_root

    return stage


####################################################################
#
@pytest.fixture
def opened() -> Callable[[Path], Archive]:
    """
    Open the staging archive a root holds, for reading back.

    A command is given the root and settles the rest itself, so a test
    checking what one wrote has to name the same archive the command
    chose.  Doing that by hand in every test would spread one decision
    across the suite.
    """

    def open_archive(archive_root: Path) -> Archive:
        """The default staging archive under this root."""
        return Archive(staged_path(archive_root, DEFAULT_ARCHIVE))

    return open_archive
