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

# Two trips whose names share a word and differ in another.  Both halves
# are load-bearing: 'kyoto' has to pick one trip and 'japan' has to pick
# both, which is what makes one of them a match and the other ambiguous.
#
OSAKA = "Osaka, Japan, May 2024"
KYOTO = "Kyoto, Japan, June 2024"

# Two trips eight months apart.  The names say which is which, so a test
# reads its own output without working the dates out.
#
EARLIER = "Earlier trip"
LATER = "Later trip"


####################################################################
#
@pytest.fixture
def runner() -> CliRunner:
    """A Click runner for the command group."""
    return CliRunner()


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
        b.trip(name=OSAKA, objects=[b.flight()]),
        b.trip(
            name=KYOTO,
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
    Stage trips into a fresh archive and give back its root.

    Every command but the staging ones needs an archive to work on
    rather than an export, so building one is groundwork rather than
    part of any test.
    """

    def stage(*trips: dict) -> Path:
        """Stage these trips, or one ordinary one when none are named."""
        if not trips:
            trips = (b.trip(objects=[b.flight()]),)
        export = write_export(*trips)
        archive_root = tmp_path / "archive"
        result = runner.invoke(
            main,
            ["stage-export", str(export), "--archive", str(archive_root)],
        )
        assert result.exit_code == 0, result.output
        return archive_root

    return stage
