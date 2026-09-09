#!/usr/bin/env python
#
"""Smoke tests for the package skeleton."""

# 3rd party imports
import pytest

# Project imports
import tripsy_exim


########################################################################
########################################################################
#
class TestPackage:
    """Tests that the package and its subpackages are importable."""

    ####################################################################
    #
    def test_version_is_set(self) -> None:
        """
        GIVEN: the installed package
        WHEN:  its version is read
        THEN:  it is a non-empty string
        """
        assert isinstance(tripsy_exim.__version__, str)
        assert tripsy_exim.__version__

    ####################################################################
    #
    @pytest.mark.parametrize(
        "name",
        ["api", "models", "sources", "store", "sync", "cli"],
    )
    def test_subpackage_imports(self, name: str) -> None:
        """
        GIVEN: the package layout
        WHEN:  each subpackage is imported
        THEN:  the import succeeds
        """
        __import__(f"tripsy_exim.{name}")
