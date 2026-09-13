#!/usr/bin/env python
#
"""
Guard the public repository against real personal data.

`.gitignore` blocks `*.ics` and `*.json` but re-allows both under
`tests/fixtures/`, so a real calendar or a real GDPR export copied into
that directory would be committed to a public repo without complaint.
That is the accident these tests exist to catch.

They are split deliberately.  The structural checks run everywhere,
including CI, and catch a real export by its own markers.  The overlap
check needs a real export to compare against and skips loudly without
one -- a guard that silently passes because its input is missing guards
nothing.

Where that export lives is deliberately not written down here.  Set
`TRIPSY_EXIM_REAL_EXPORT` to the directory holding it; a public repo
should not name a path on anyone's disk.

The structural check is the stronger of the two: an overlap check only
catches a verbatim copy, while PRODID and the UID domain identify a real
export whatever it contains.
"""

# system imports
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

# 3rd party imports
import pytest
import pytest_check as check
from icalendar import Calendar

# Project imports
from tests.factories import CollaboratorPayloadFactory
from tests.ics_builder import (
    SYNTHETIC_PRODID,
    SYNTHETIC_UID_DOMAIN,
    events_of,
    field,
    uid_of,
)
from tests.tripit_builder import SYNTHETIC_MARKER, SYNTHETIC_MARKER_VALUE

FIXTURES = Path(__file__).parent / "fixtures"

# Reference material only.  Never committed, and absent in CI, so the
# overlap check is opt-in through the environment rather than through a
# path this file would otherwise have to name.
#
REAL_EXPORT_ENV = "TRIPSY_EXIM_REAL_EXPORT"

# Reserved by RFC 2606 and RFC 6761, so they can never reach a real inbox.
#
SAFE_EMAIL_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "example.invalid",
)


####################################################################
#
def fixture_calendars() -> list[Path]:
    """Every .ics committed under tests/fixtures/."""
    return sorted(FIXTURES.rglob("*.ics"))


####################################################################
#
def fixture_exports() -> list[Path]:
    """Every .json committed under tests/fixtures/."""
    return sorted(FIXTURES.rglob("*.json"))


########################################################################
########################################################################
#
class TestCommittedFixturesAreSynthetic:
    """Runs everywhere, including where the real export is absent."""

    ####################################################################
    #
    def test_there_is_something_to_check(self) -> None:
        """
        GIVEN: the fixtures directory
        WHEN:  it is listed
        THEN:  at least one calendar is present, so the checks below are
               not passing merely because they found nothing
        """
        assert fixture_calendars(), f"no .ics fixtures under {FIXTURES}"

    ####################################################################
    #
    @pytest.mark.parametrize("path", fixture_calendars(), ids=lambda p: p.name)
    def test_a_fixture_carries_the_synthetic_markers(self, path: Path) -> None:
        """
        GIVEN: a calendar committed under tests/fixtures/
        WHEN:  its PRODID and UIDs are read
        THEN:  both are the generator's, which a real export cannot match
        """
        text = path.read_text(encoding="utf-8")
        calendar = Calendar.from_ical(text)
        events = events_of(text)

        check.equal(
            str(field(calendar, "PRODID")), SYNTHETIC_PRODID, "generated PRODID"
        )
        check.is_true(events, "the calendar has events at all")
        for event in events:
            uid = uid_of(event)
            check.is_true(
                uid.endswith(f"@{SYNTHETIC_UID_DOMAIN}"),
                f"UID domain in {path.name}",
            )

    ####################################################################
    #
    @pytest.mark.parametrize("path", fixture_exports(), ids=lambda p: p.name)
    def test_an_export_fixture_carries_the_synthetic_marker(
        self, path: Path
    ) -> None:
        """
        GIVEN: a GDPR-export-shaped .json committed under tests/fixtures/
        WHEN:  its top level is read
        THEN:  it carries the generator's marker, which a real export
               cannot

        With no such fixture committed this reports as skipped rather
        than passed, which is the honest outcome -- and it fires the
        moment one appears.
        """
        document = json.loads(path.read_text(encoding="utf-8"))

        assert isinstance(document, dict), f"{path.name} is not an object"
        assert document.get(SYNTHETIC_MARKER) == SYNTHETIC_MARKER_VALUE, (
            f"{path.name} carries no generated-export marker"
        )

    ####################################################################
    #
    @pytest.mark.parametrize("path", fixture_calendars(), ids=lambda p: p.name)
    def test_a_fixture_never_claims_to_come_from_tripit(
        self, path: Path
    ) -> None:
        """
        GIVEN: a calendar committed under tests/fixtures/
        WHEN:  its text is searched
        THEN:  tripit.com appears nowhere, since every real UID carries it
        """
        assert "tripit.com" not in path.read_text(encoding="utf-8").lower()

    ####################################################################
    #
    def test_generated_calendars_are_synthetic_too(
        self, ics_calendar: Callable[..., str]
    ) -> None:
        """
        GIVEN: a freshly generated calendar
        WHEN:  its markers are read
        THEN:  they match what the fixtures are held to, so a regenerated
               fixture cannot drift out of the guard's reach
        """
        text = ics_calendar(items=3)

        check.is_in(SYNTHETIC_PRODID, text, "PRODID")
        check.is_in(SYNTHETIC_UID_DOMAIN, text, "UID domain")
        check.is_not_in("tripit.com", text.lower(), "no TripIt marker")

    ####################################################################
    #
    def test_generated_emails_use_reserved_domains(self) -> None:
        """
        GIVEN: a collaborator built by the factories
        WHEN:  its email is read
        THEN:  the domain is one reserved for documentation, so a fixture
               can never name a reachable address
        """
        for _ in range(20):
            built = cast(dict[str, Any], CollaboratorPayloadFactory())
            email = str(built["email"])
            assert email.endswith(SAFE_EMAIL_DOMAINS), email


########################################################################
########################################################################
#
class TestNoOverlapWithTheRealExport:
    """
    Runs only where a real export is configured, and says so otherwise.

    A skip is the honest outcome here: the check has no input, and
    reporting a pass would be a lie about what was verified.
    """

    ####################################################################
    #
    @pytest.fixture
    def real_export(self) -> Path:
        """The configured export, or a loud skip rather than a vacuous pass."""
        configured = os.environ.get(REAL_EXPORT_ENV, "")
        if not configured:
            pytest.skip(
                f"{REAL_EXPORT_ENV} is unset -- reference material is never "
                "committed, so this check cannot run here"
            )
        export = Path(configured)
        if not export.is_dir() or not any(export.glob("*.ics")):
            pytest.skip(
                f"{REAL_EXPORT_ENV} does not name a directory of "
                "calendars, so this check cannot run here"
            )
        return export

    ####################################################################
    #
    def test_no_fixture_reuses_a_real_uid(self, real_export: Path) -> None:
        """
        GIVEN: the real export alongside the committed fixtures
        WHEN:  their UIDs are compared
        THEN:  no fixture shares one, which a copied file would
        """
        real: set[str] = set()
        for path in real_export.glob("*.ics"):
            real.update(
                uid_of(e)
                for e in events_of(
                    path.read_text(encoding="utf-8", errors="replace")
                )
            )

        for path in fixture_calendars():
            mine = {
                uid_of(e) for e in events_of(path.read_text(encoding="utf-8"))
            }
            assert not (mine & real), f"{path.name} reuses a real UID"

    ####################################################################
    #
    def test_no_fixture_reuses_real_summary_text(
        self, real_export: Path
    ) -> None:
        """
        GIVEN: the real export alongside the committed fixtures
        WHEN:  their event summaries are compared
        THEN:  none is shared, catching a copy whose UIDs were rewritten
        """
        real: set[str] = set()
        for path in real_export.glob("*.ics"):
            real.update(
                str(e.get("SUMMARY", ""))
                for e in events_of(
                    path.read_text(encoding="utf-8", errors="replace")
                )
            )
        real.discard("")

        for path in fixture_calendars():
            mine = {
                str(e.get("SUMMARY", ""))
                for e in events_of(path.read_text(encoding="utf-8"))
            }
            assert not (mine & real), f"{path.name} reuses real summary text"
