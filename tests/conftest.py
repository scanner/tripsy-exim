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
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

# 3rd party imports
import factory.random
import httpx
import pytest
from faker import Faker
from pytest_factoryboy import register
from pytest_mock import MockerFixture

# Project imports
from tests import ics_builder
from tests.clock import FakeClock
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
from tests.fake_tripsy import BASE, FakeTripsy, transport
from tripsy_exim.api import IMPORT, RetryPolicy, TripsyClient
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


####################################################################
#
@pytest.fixture
def fake_tripsy() -> FakeTripsy:
    """An empty in-memory Tripsy, with expense permission."""
    return FakeTripsy()


####################################################################
#
@pytest.fixture
def restricted_tripsy() -> FakeTripsy:
    """
    A Tripsy that withholds price and currency.

    The real account is premium, so this response cannot be observed from
    outside -- this fixture is the only way that path is ever exercised.
    """
    return FakeTripsy(can_see_expenses=False)


####################################################################
#
@pytest.fixture
def tripsy_client(fake_tripsy: FakeTripsy) -> Iterator[httpx.Client]:
    """An httpx client wired to the fake, needing no credentials."""
    with httpx.Client(
        base_url=BASE, transport=transport(fake_tripsy)
    ) as client:
        yield client


####################################################################
#
@pytest.fixture
def ics_calendar(faker: Faker) -> Callable[..., str]:
    """
    Build synthetic TripIt-shaped .ics text.

    Takes the same keyword arguments as `ics_builder.build_calendar`, so a
    test asks for the defects it wants to exercise.
    """

    def build(**kwargs: Any) -> str:
        return ics_builder.to_ics(ics_builder.build_calendar(faker, **kwargs))

    return build


####################################################################
#
@pytest.fixture
def clock(mocker: MockerFixture) -> FakeClock:
    """
    Put a controllable clock under the pacer for one test.

    `monotonic` and `sleep` are patched where `pacing` imported them, not
    on the `time` module itself, so nothing outside that module is
    affected -- patching `time.monotonic` globally would reach pytest and
    httpx as well.  `mocker` undoes both when the test ends.

    Any test that exercises pacing must ask for this fixture.  Without it
    a pacer runs on the real clock and a test asserting a thirty-second
    back-off would take thirty seconds.

    Returns:
        The clock now in force, which a test moves with `advance` and
        reads through `slept`.
    """
    fake = FakeClock()
    mocker.patch("tripsy_exim.api.pacing.monotonic", fake.monotonic)
    mocker.patch("tripsy_exim.api.pacing.sleep", fake.sleep)
    return fake


####################################################################
#
def paced_store(clock: FakeClock, **kwargs: Any) -> FakeTripsy:
    """A fake wired to the test clock, for tests that need their own."""
    return FakeTripsy(clock=clock, **kwargs)


####################################################################
#
@pytest.fixture
def paced_tripsy(clock: FakeClock) -> FakeTripsy:
    """A fake whose response latency is charged to the test clock."""
    return paced_store(clock)


####################################################################
#
def client_for(
    store: FakeTripsy, clock: FakeClock, **kwargs: Any
) -> TripsyClient:
    """
    Build a client onto a particular store.

    The clock is not handed to the client -- it is already patched under
    the pacer by the `clock` fixture.  It stays in the signature so that
    a caller cannot build a paced client without having asked for the
    fixture that makes one testable.

    Jitter is pinned to zero so a back-off is one number a test can
    assert rather than a range.

    Args:
        store: The fake to answer from.
        clock: The patched clock, taken for the dependency rather than
            for its value.
        kwargs: Passed to `TripsyClient`, e.g. `profile` or `timeout`.

    Returns:
        A client that needs closing, or using as a context manager.
    """
    kwargs.setdefault("retries", RetryPolicy(jitter=0.0))
    return TripsyClient(
        token="token-for-tests",
        transport=transport(store),
        **kwargs,
    )


####################################################################
#
@pytest.fixture
def api_client(
    paced_tripsy: FakeTripsy, clock: FakeClock
) -> Iterator[TripsyClient]:
    """A client on the fake, paced by the import profile."""
    with client_for(paced_tripsy, clock, profile=IMPORT) as client:
        yield client
