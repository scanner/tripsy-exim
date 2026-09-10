#!/usr/bin/env python
#
"""
Fixtures shared across the test modules.

`register` turns each factory into fixtures.  Payload factories subclass
`DictFactory`, whose model is `dict`, so each needs an explicit name or
they would all collide on one fixture called `dict`.

Any declared field can be overridden per test by parametrising
`<fixture>__<field>` directly -- no `indirect=`, which silently does
nothing here.  The `<fixture>_factory` form builds ad-hoc variants,
including fields the factory does not declare.
"""

# system imports
import os
from pathlib import Path

# 3rd party imports
import factory.random
import pytest
from pytest_factoryboy import register

# Project imports
from tests.factories import (
    ActivityPayloadFactory,
    CollaboratorPayloadFactory,
    ExpensePayloadFactory,
    HostingFactory,
    HostingPayloadFactory,
    TransportationPayloadFactory,
    TripFactory,
    TripPayloadFactory,
)
from tripsy_exim.store import Archive

# Wire-shaped payloads, as the API returns them.
#
# TripPayloadFactory -> fixtures trip_payload, trip_payload_factory
register(TripPayloadFactory, "trip_payload")
# HostingPayloadFactory -> fixtures hosting_payload, hosting_payload_factory
register(HostingPayloadFactory, "hosting_payload")
# ActivityPayloadFactory -> fixtures activity_payload,
#     activity_payload_factory
register(ActivityPayloadFactory, "activity_payload")
# TransportationPayloadFactory -> fixtures transportation_payload,
#     transportation_payload_factory
register(TransportationPayloadFactory, "transportation_payload")
# ExpensePayloadFactory -> fixtures expense_payload, expense_payload_factory
register(ExpensePayloadFactory, "expense_payload")
# CollaboratorPayloadFactory -> fixtures collaborator_payload,
#     collaborator_payload_factory
register(CollaboratorPayloadFactory, "collaborator_payload")

# Canonical objects, built rather than parsed.
#
# TripFactory -> fixtures trip, trip_factory
register(TripFactory)
# HostingFactory -> fixtures hosting, hosting_factory
register(HostingFactory)


# One seed behind every generator in the suite.  Override it to shake out
# a test that has quietly come to depend on a particular value:
#
#     TRIPSY_TEST_SEED=12345 make test
#
TEST_SEED = int(os.environ.get("TRIPSY_TEST_SEED", "20260909"))


####################################################################
#
@pytest.fixture(scope="session")
def faker_seed() -> int:
    """
    Seed the `faker` fixture.

    Faker's plugin looks up this exact fixture name, and only when it is in
    the active fixture closure -- which is why `_seed_random_data` below
    depends on it rather than reading TEST_SEED directly.
    """
    return TEST_SEED


####################################################################
#
@pytest.fixture(scope="session", autouse=True)
def _seed_random_data(faker_seed: int) -> None:
    """
    Seed factory_boy's generator once for the whole session.

    `factory.Faker` draws from factory_boy's own generator rather than the
    one the `faker` fixture holds, so both are pinned to the same value
    here and every generated value in the suite descends from it.
    """
    factory.random.reseed_random(faker_seed)


####################################################################
#
@pytest.fixture
def archive(tmp_path: Path) -> Archive:
    """An empty archive rooted in a temporary directory."""
    return Archive(tmp_path / "archive")
