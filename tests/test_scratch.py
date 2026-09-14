#!/usr/bin/env python
#
"""Test the throwaway identifier namespace."""

# system imports
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check
from faker import Faker

# Project imports
from tests.ics_builder import build_calendar, to_ics
from tripsy_exim.models import (
    IDENTIFIER_PREFIX,
    is_minted,
    is_scratch,
    mint,
    scratch_namespace,
)
from tripsy_exim.sources import parse
from tripsy_exim.store import Archive, local_key
from tripsy_exim.sync import stage


####################################################################
#
def calendar_text(faker: Faker, **kwargs: Any) -> str:
    """One synthetic calendar, as a .ics document."""
    return to_ics(build_calendar(faker, **kwargs))


########################################################################
########################################################################
#
class TestScratchNamespace:
    """Tests for building the namespace a throwaway run mints into."""

    ####################################################################
    #
    def test_generated_run_is_unique(self) -> None:
        """
        GIVEN: no run token
        WHEN:  two namespaces are built
        THEN:  they differ, so two runs cannot collide
        """
        check.not_equal(scratch_namespace(), scratch_namespace())

    ####################################################################
    #
    def test_given_run_is_used(self) -> None:
        """
        GIVEN: a run token
        WHEN:  a namespace is built from it
        THEN:  the token appears in the namespace
        """
        check.equal(scratch_namespace("abc123"), "scratch-abc123")

    ####################################################################
    #
    @pytest.mark.parametrize(
        "token", ["", "has-hyphen", "has space", "sl/ash"], ids=repr
    )
    def test_bad_run_tokens_are_refused(self, token: str) -> None:
        """
        GIVEN: a run token that would not survive being a filename
        WHEN:  a namespace is built from it
        THEN:  it is refused rather than producing an unreadable key
        """
        with pytest.raises(ValueError):
            scratch_namespace(token)


########################################################################
########################################################################
#
class TestIsScratch:
    """Tests for telling throwaway identifiers from real ones."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "identifier,expected",
        [
            pytest.param(
                mint(scratch_namespace("r1"), "uid"), True, id="scratch"
            ),
            pytest.param(mint("tripit-uid", "uid"), False, id="real"),
            pytest.param(None, False, id="none"),
            pytest.param("", False, id="empty"),
            pytest.param("scratch-r1-abc", False, id="unprefixed"),
            pytest.param(
                f"{IDENTIFIER_PREFIX}-scratchy-abc", False, id="near-miss"
            ),
        ],
    )
    def test_recognises_scratch_identifiers(
        self, identifier: str | None, expected: bool
    ) -> None:
        """
        GIVEN: an identifier
        WHEN:  it is tested for belonging to a throwaway run
        THEN:  only identifiers minted into a scratch namespace match
        """
        check.equal(is_scratch(identifier), expected)

    ####################################################################
    #
    def test_scratch_identifiers_are_still_minted(self) -> None:
        """
        GIVEN: an identifier from a throwaway run
        WHEN:  it is tested for being one of ours
        THEN:  it is, so the archive keys it by identifier not Tripsy id
        """
        check.is_true(is_minted(mint(scratch_namespace("r1"), "uid")))


########################################################################
########################################################################
#
class TestScratchStaging:
    """Tests for staging a calendar under a throwaway namespace."""

    ####################################################################
    #
    def test_keys_survive_being_filenames(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: a calendar staged under a scratch namespace
        WHEN:  its objects are keyed for the archive
        THEN:  the key is the identifier unchanged

        Purging a run means matching the prefix on disk, so the on-disk
        key has to be the identifier and not a sanitised version of it.
        """
        archive = Archive(tmp_path)
        parsed = parse(calendar_text(faker, items=3), scratch_namespace("run1"))

        staged = stage(archive, parsed)

        check.equal(staged.trip_key, parsed.trip.internal_identifier)
        check.is_true(staged.trip_key.startswith(f"{IDENTIFIER_PREFIX}-"))
        for activity in parsed.activities:
            check.equal(local_key(activity), activity.internal_identifier)

    ####################################################################
    #
    def test_a_run_can_be_found_by_prefix(
        self, tmp_path: Path, faker: Faker
    ) -> None:
        """
        GIVEN: one real staging run and two scratch runs
        WHEN:  the archive is searched by namespace prefix
        THEN:  each run's trips are found without touching the others
        """
        archive = Archive(tmp_path)
        text = calendar_text(faker, items=3)

        stage(archive, parse(text))
        stage(archive, parse(text, scratch_namespace("runa")))
        stage(archive, parse(text, scratch_namespace("runb")))

        keys = archive.trip_keys()
        scratch = [k for k in keys if is_scratch(k)]
        check.equal(len(keys), 3)
        check.equal(len(scratch), 2)
        check.equal(len([k for k in keys if "scratch-runa" in k]), 1)
