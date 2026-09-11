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

# Enough to keep two shaping runs on the same day apart.  Matches the
# length the probe scripts use.
#
_RUN_TOKEN_CHARS = 8


####################################################################
#
def mint(namespace: str, *parts: str) -> str:
    """
    Derive a stable `internal_identifier` from source data.

    Args:
        namespace: The source the parts came from, e.g. 'ics' or 'tripit'.
        parts: Values that together identify one record in that source --
            an .ics VEVENT UID, a TripIt record id.

    Returns:
        An identifier of the form 'txim-<namespace>-<16 hex digits>'.

    Raises:
        ValueError: If the namespace is empty, or no parts were given.
    """
    if not namespace:
        raise ValueError("namespace must not be empty")
    if not parts or not any(parts):
        raise ValueError("at least one non-empty part is required")

    # NUL separates the parts so that ('a', 'bc') and ('ab', 'c') do not
    # collide onto the same identifier.
    #
    joined = "\0".join(parts).encode("utf-8")
    digest = blake2s(joined, digest_size=_DIGEST_BYTES).hexdigest()
    return f"{IDENTIFIER_PREFIX}-{namespace}-{digest}"


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
        'txim-scratch-<run>-<digest>'.

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
