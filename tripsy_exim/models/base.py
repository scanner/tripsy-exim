#!/usr/bin/env python
#
"""
The base every canonical model is built on.

Three behaviours live here because all six models need them identically:
unset is distinguishable from null, unknown fields are retained rather than
rejected, and money is a Decimal.

Write payloads are projected through a per-model `WRITABLE` set.  Models
carry read-only fields the API returns and never accepts back, so a payload
built by dumping a whole object would be rejected or, worse, quietly
misinterpreted.
"""

# system imports
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Self, TypeVar

# 3rd party imports
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict

# Source data Tripsy has nowhere to put is namespaced under this one key
# inside the passthrough container, which is what keeps it separable from
# the undocumented fields Tripsy itself returns.  The importer sends
# neither, but only this key is knowable in advance.
#
SOURCE_KEY = "x_source"

# Fields the API returns as a JSON float and accepts back as a JSON number.
# They are Decimal in memory and on disk; `writable_payload` converts them
# back at the boundary.
#
MONEY_FIELDS = frozenset({"price"})

M = TypeVar("M", bound="CanonicalModel")


####################################################################
#
def _require_utc(value: datetime) -> datetime:
    """Normalise an aware datetime to UTC, rejecting naive ones."""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


####################################################################
#
def _float_to_decimal(value: Any) -> Any:
    """Route a JSON float through str so 78.5 does not become 78.5000000001."""
    if isinstance(value, float):
        return Decimal(str(value))
    return value


# Child objects timestamp in UTC; trip `starts_at`/`ends_at` are plain dates
# and use `date` directly.
#
UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]
Money = Annotated[Decimal, BeforeValidator(_float_to_decimal)]


########################################################################
########################################################################
#
class CanonicalModel(BaseModel):
    """Provider-neutral base for every canonical object."""

    model_config = ConfigDict(
        extra="allow",
        validate_assignment=True,
    )

    # Fields the object's own v1 endpoint accepts on create and update.
    # `update_trip` is deliberately absent from every subclass: on a
    # PUT/PATCH it moves the object to a different trip.
    #
    WRITABLE: ClassVar[frozenset[str]] = frozenset()

    ####################################################################
    #
    @property
    def wire_extras(self) -> dict[str, Any]:
        """Undocumented fields Tripsy returned, verbatim."""
        extra = self.model_extra or {}
        return {k: v for k, v in extra.items() if k != SOURCE_KEY}

    ####################################################################
    #
    @property
    def source_extras(self) -> dict[str, Any]:
        """Source fields Tripsy has no home for, retained for the archive."""
        value = (self.model_extra or {}).get(SOURCE_KEY, {})
        return value if isinstance(value, dict) else {}

    ####################################################################
    #
    def with_source(self, **fields: Any) -> Self:
        """Return a copy carrying additional source-only fields."""
        merged = {**self.source_extras, **fields}
        return self.model_copy(update={SOURCE_KEY: merged})

    ####################################################################
    #
    def writable_payload(self) -> dict[str, Any]:
        """
        Build the JSON body for a create or update of this object.

        Only fields that were explicitly set are included, so a PATCH built
        from a partially populated model touches nothing else.

        Returns:
            A JSON-ready dict restricted to this model's writable fields.
        """
        dumped = self.model_dump(mode="json", exclude_unset=True)
        payload = {k: v for k, v in dumped.items() if k in self.WRITABLE}

        # Decimal serialises to a JSON string, which the API would reject
        # for a numeric field.
        #
        for name in MONEY_FIELDS & payload.keys():
            if payload[name] is not None:
                payload[name] = float(payload[name])
        return payload

    ####################################################################
    #
    def merged_with(self, incoming: Self) -> Self:
        """
        Overlay another instance of the same model onto this one.

        Only fields the incoming object actually set participate, so a
        response that omitted `price` because the caller cannot see
        expenses leaves a previously captured price intact.  A field set to
        null does overwrite -- null is a value, absence is not.

        Args:
            incoming: The newer, possibly partial, version of this object.

        Returns:
            A new instance whose set fields are the union of both.
        """
        if type(self) is not type(incoming):
            raise TypeError(
                f"cannot merge {type(incoming).__name__} into "
                f"{type(self).__name__}"
            )

        data = self.model_dump(exclude_unset=True)
        data.update(incoming.model_dump(exclude_unset=True))

        # Source fields accumulate across merges; a later import that knew
        # about fewer of them must not drop the rest.
        #
        source = {**self.source_extras, **incoming.source_extras}
        if source:
            data[SOURCE_KEY] = source

        return type(self).model_validate(data)
