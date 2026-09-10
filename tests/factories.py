#!/usr/bin/env python
#
"""
Factories for synthetic Tripsy payloads.

Two kinds live here.  The payload factories build dicts shaped like the
JSON the API actually returns -- prices as floats, datetimes as strings --
so tests exercise the same parsing path a real response would.  Model
factories build canonical objects directly, for tests that start from an
object rather than a response.

Fields a test asserts on are fixed; the rest come from Faker, so a test
cannot quietly couple itself to a value it never meant to pin.  Faker
output is generated per run and never written to the repository, and its
email provider uses the reserved `example.*` domains.  `conftest` reseeds
factory_boy so a failure reproduces.

`SKIP` is re-exported because omitting a field and nulling it are different
things here.  Parametrising `hosting_payload__price` with it produces a
payload carrying no price at all, which is what a caller without expense
permission sees.

Everything generated here is invented.  No real trip, itinerary, address,
confirmation code, or name is allowed into the test suite.
"""

# system imports
from decimal import Decimal

# 3rd party imports
import factory
from factory.declarations import SKIP
from pytest_factoryboy import named_model

# Project imports
from tripsy_exim.models import Hosting, Trip, mint

__all__ = [
    "SKIP",
    "ActivityPayloadFactory",
    "CollaboratorPayloadFactory",
    "ExpensePayloadFactory",
    "HostingFactory",
    "HostingPayloadFactory",
    "TransportationPayloadFactory",
    "TripFactory",
    "TripPayloadFactory",
]


########################################################################
########################################################################
#
class TripPayloadFactory(factory.DictFactory):
    """A trip as `GET /v1/trips` returns it.  Bounds are plain dates."""

    class Meta:
        model = named_model(dict, "TripPayload")

    id = factory.Sequence(lambda n: 42 + n)
    internal_identifier = factory.Sequence(lambda n: mint("ics", f"trip-{n}"))
    name = factory.Faker("city")
    description = factory.Faker("sentence")
    timezone = "Europe/Rome"
    starts_at = "2027-06-01"
    ends_at = "2027-06-15"
    has_dates = True


########################################################################
########################################################################
#
class HostingPayloadFactory(factory.DictFactory):
    """A hosting as the API returns it.  Datetimes are UTC strings."""

    class Meta:
        model = named_model(dict, "HostingPayload")

    id = factory.Sequence(lambda n: 101 + n)
    internal_identifier = factory.Sequence(
        lambda n: mint("ics", f"hosting-{n}")
    )
    trip = 42
    name = factory.Faker("company")
    address = factory.Faker("street_address")
    phone = factory.Faker("phone_number")
    starts_at = "2027-06-01T14:00:00Z"
    ends_at = "2027-06-05T11:00:00Z"
    timezone = "Europe/Rome"
    price = 78.5
    currency = "EUR"
    created_at = "2027-03-17T14:30:00Z"
    updated_at = "2027-03-17T14:30:00Z"


########################################################################
########################################################################
#
class ActivityPayloadFactory(factory.DictFactory):
    """An activity as the API returns it."""

    class Meta:
        model = named_model(dict, "ActivityPayload")

    id = factory.Sequence(lambda n: 202 + n)
    internal_identifier = factory.Sequence(
        lambda n: mint("ics", f"activity-{n}")
    )
    trip = 42
    name = factory.Faker("sentence", nb_words=3)
    address = factory.Faker("street_address")
    activity_type = "sightseeing"
    starts_at = "2027-06-03T09:00:00Z"
    ends_at = "2027-06-03T11:00:00Z"
    timezone = "Europe/Rome"
    price = 45.0
    currency = "EUR"


########################################################################
########################################################################
#
class TransportationPayloadFactory(factory.DictFactory):
    """
    A transportation leg as the API returns it.

    Departure and arrival are deliberately in different timezones, since
    the pair is what makes local wall-clock time recoverable.
    """

    class Meta:
        model = named_model(dict, "TransportationPayload")

    id = factory.Sequence(lambda n: 303 + n)
    internal_identifier = factory.Sequence(
        lambda n: mint("ics", f"transport-{n}")
    )
    trip = 42
    name = factory.Faker("sentence", nb_words=3)
    company = factory.Faker("company")
    transportation_type = "airplane"
    departure_at = "2027-05-31T22:30:00Z"
    departure_timezone = "America/New_York"
    arrival_at = "2027-06-01T10:30:00Z"
    arrival_timezone = "Europe/Rome"


########################################################################
########################################################################
#
class ExpensePayloadFactory(factory.DictFactory):
    """An expense as the API returns it.  It carries no identifier."""

    class Meta:
        model = named_model(dict, "ExpensePayload")

    id = factory.Sequence(lambda n: 404 + n)
    title = factory.Faker("sentence", nb_words=2)
    date = "2027-06-03T20:00:00Z"
    price = 12.25
    currency = "EUR"
    trip = 42


########################################################################
########################################################################
#
class CollaboratorPayloadFactory(factory.DictFactory):
    """A collaborator as the API returns it, permissions nested."""

    class Meta:
        model = named_model(dict, "CollaboratorPayload")

    id = factory.Sequence(lambda n: 2 + n)
    name = factory.Faker("name")
    email = factory.Faker("email")
    photo_url = None
    joined = True
    permissions = factory.LazyFunction(
        lambda: {
            "is_owner": False,
            "can_edit": True,
            "can_see_expenses": True,
            "can_edit_expenses": True,
            "is_travelling": True,
            "receive_notifications": True,
        }
    )


########################################################################
########################################################################
#
class TripFactory(factory.Factory):
    """
    A canonical trip built directly, not parsed from a response.

    Only the fields declared here are set, so `model_fields_set` stays
    meaningful and a built object behaves like a partial one.
    """

    class Meta:
        model = Trip

    id = factory.Sequence(lambda n: 42 + n)
    internal_identifier = factory.Sequence(lambda n: mint("ics", f"trip-{n}"))
    name = factory.Faker("city")
    timezone = "Europe/Rome"


########################################################################
########################################################################
#
class HostingFactory(factory.Factory):
    """
    A canonical hosting built directly.

    `price` is a Decimal here rather than the float the wire carries --
    this is the domain side of that boundary.
    """

    class Meta:
        model = Hosting

    id = factory.Sequence(lambda n: 101 + n)
    internal_identifier = factory.Sequence(
        lambda n: mint("ics", f"hosting-{n}")
    )
    name = factory.Faker("company")
    starts_at = "2027-06-01T14:00:00Z"
    ends_at = "2027-06-05T11:00:00Z"
    timezone = "Europe/Rome"
    price = Decimal("78.5")
    currency = "EUR"
