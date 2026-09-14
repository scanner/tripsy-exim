#!/usr/bin/env python
#
"""
Minting `internal_identifier` values.

`internal_identifier` is the API's idempotency key: re-POSTing an object
whose identifier already exists returns an empty 200 instead of creating a
second one.  Minting it deterministically from the source data is what
makes a re-run of an import a no-op rather than a duplicate.

Identifiers are derived, never random, so the same source record always
produces the same identifier -- across runs, machines, and rebuilds of the
archive.
"""

# system imports
import re
from hashlib import blake2s
from uuid import uuid4

# Marks an identifier as ours.  Objects created in the Tripsy app carry
# either no identifier or one we did not mint, and the archive keys them
# differently.
#
IDENTIFIER_PREFIX = "txim"

# Trip-level duplicate suppression only engages above 5 characters.  The
# prefix alone clears that, and the digest leaves a wide margin.
#
_DIGEST_BYTES = 8

# Marks a namespace as belonging to a throwaway run.
#
SCRATCH_PREFIX = "scratch"

# Marks the generation segment.  Tripsy never releases an identifier: a
# deleted object keeps its own, so re-importing it creates nothing.  The
# only way back is to mint a *different* identifier for the same source
# record, which is what the generation is for -- generation 2 of a record
# is a second attempt at it after the first was spent.
#
# The letter matters.  Without it the segment would be bare digits, which
# are also valid hex and a legal scratch run token, and nothing could tell
# a generation from part of the namespace or the digest.
#
GENERATION_PREFIX = "g"

# The first generation.  Every identifier carries one, so the segment is
# never absent and a reader never has to guess what an older identifier
# meant.
#
FIRST_GENERATION = 1

_GENERATION = re.compile(
    rf"-{GENERATION_PREFIX}(\d+)-[0-9a-f]{{{_DIGEST_BYTES * 2}}}$"
)

# Enough to keep two shaping runs on the same day apart.  Matches the
# length the probe scripts use.
#
_RUN_TOKEN_CHARS = 8


####################################################################
#
def mint(
    namespace: str, *parts: str, generation: int = FIRST_GENERATION
) -> str:
    """
    Derive a stable `internal_identifier` from source data.

    Args:
        namespace: The source the parts came from, e.g. 'ics' or 'tripit'.
        parts: Values that together identify one record in that source --
            an .ics VEVENT UID, a TripIt record id.
        generation: Which attempt at this record the identifier is for.
            The first is 1 and is what every import uses; a later one is
            how a record whose identifier was spent gets imported again,
            since Tripsy will not accept the spent one.

    Returns:
        An identifier of the form 'txim-<namespace>-g<NN>-<16 hex digits>'.

    Raises:
        ValueError: If the namespace is empty, no parts were given, or
            the generation is below one.
    """
    if not namespace:
        raise ValueError("namespace must not be empty")
    if not parts or not any(parts):
        raise ValueError("at least one non-empty part is required")
    if generation < FIRST_GENERATION:
        raise ValueError(
            f"generation must be {FIRST_GENERATION} or greater, "
            f"got {generation}"
        )

    # NUL separates the parts so that ('a', 'bc') and ('ab', 'c') do not
    # collide onto the same identifier.
    #
    # The generation is deliberately outside the digest.  Two generations
    # of one record have to be recognisably the same record, which they
    # would not be if the counter changed the hash.
    #
    joined = "\0".join(parts).encode("utf-8")
    digest = blake2s(joined, digest_size=_DIGEST_BYTES).hexdigest()
    return (
        f"{IDENTIFIER_PREFIX}-{namespace}-"
        f"{GENERATION_PREFIX}{generation:02d}-{digest}"
    )


####################################################################
#
def generation_of(identifier: str | None) -> int | None:
    """
    Read the generation out of an identifier.

    Args:
        identifier: Any identifier, or None.

    Returns:
        The generation, or None when this is not one `mint` produced.
    """
    if not identifier:
        return None
    found = _GENERATION.search(identifier)
    return int(found.group(1)) if found else None


####################################################################
#
def namespace_of(identifier: str | None) -> str | None:
    """
    Read the namespace out of an identifier.

    An archive is self-describing: an object added to a staged trip by
    hand takes its namespace from the trip it is added to, so a shaping
    run's additions mint shaping identifiers without being told which
    run they belong to.

    Args:
        identifier: Any identifier, or None.

    Returns:
        The namespace, or None when this is not one `mint` produced.
    """
    if not is_minted(identifier) or not identifier:
        return None
    found = _GENERATION.search(identifier)
    if not found:
        return None
    body = identifier[len(IDENTIFIER_PREFIX) + 1 : found.start()]
    return body or None


####################################################################
#
def next_generation(identifier: str) -> str:
    """
    The same record's identifier, one generation on.

    Used when a POST of `identifier` answers an empty 200 with no object
    behind it: the identifier was spent by something since deleted, and
    the record needs a fresh one that still derives from the same source.

    Args:
        identifier: An identifier `mint` produced.

    Returns:
        The identifier with its generation incremented.

    Raises:
        ValueError: If this is not an identifier `mint` produced.
    """
    current = generation_of(identifier)
    if current is None:
        raise ValueError(f"{identifier!r} carries no generation")

    head = identifier[: identifier.rindex(f"-{GENERATION_PREFIX}")]
    digest = identifier.rsplit("-", 1)[1]
    return f"{head}-{GENERATION_PREFIX}{current + 1:02d}-{digest}"


####################################################################
#
def scratch_namespace(run: str | None = None) -> str:
    """
    Build the namespace a throwaway run mints into.

    Tripsy never releases an identifier: deleting an object leaves the
    identifier suppressed, so re-posting it creates nothing.  A run whose
    objects will be deleted therefore has to spend identifiers the real
    import will never want, which is what a separate namespace gives it.
    The same file parsed under this namespace produces the same objects
    under a different key space.

    Args:
        run: Token naming this run.  One is generated when not given.

    Returns:
        A namespace of the form 'scratch-<run>', which `mint` turns into
        'txim-scratch-<run>-g<NN>-<digest>'.

    Raises:
        ValueError: If the run token is empty or carries characters that
            would not survive being used as a filename.
    """
    token = run if run is not None else uuid4().hex[:_RUN_TOKEN_CHARS]
    if not token:
        raise ValueError("run token must not be empty")
    if not all(c.isalnum() or c in "._" for c in token):
        raise ValueError(
            f"run token {token!r} must be alphanumeric, '.' or '_'; "
            "a hyphen would blur the boundary with the digest"
        )
    return f"{SCRATCH_PREFIX}-{token}"


####################################################################
#
def is_scratch(identifier: str | None) -> bool:
    """
    Report whether this identifier belongs to a throwaway run.

    The importer uses this to keep the two apart: scratch objects have no
    business in a real import, and real identifiers must not be spent by a
    run that is going to be deleted.
    """
    if identifier is None:
        return False
    return identifier.startswith(f"{IDENTIFIER_PREFIX}-{SCRATCH_PREFIX}-")


####################################################################
#
def is_minted(identifier: str | None) -> bool:
    """Report whether this identifier is one `mint` produced."""
    if identifier is None:
        return False
    return identifier.startswith(f"{IDENTIFIER_PREFIX}-")
