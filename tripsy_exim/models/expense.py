#!/usr/bin/env python
#
"""
The canonical expense.

Expenses have no `internal_identifier`, so the idempotency every other
object gets for free is not available here -- an importer has to match on
the fields themselves or track what it wrote.
"""

# system imports
from typing import ClassVar

# 3rd party imports
from tripsy_exim.models.base import CanonicalModel, Money, UtcDatetime


########################################################################
########################################################################
#
class Expense(CanonicalModel):
    """An expense recorded against a trip."""

    WRITABLE: ClassVar[frozenset[str]] = frozenset(
        {
            "title",
            "date",
            "price",
            "currency",
        }
    )

    id: int | None = None
    title: str | None = None
    date: UtcDatetime | None = None
    price: Money | None = None
    currency: str | None = None

    # Read-only.
    #
    owner: int | None = None
    trip: int | None = None
