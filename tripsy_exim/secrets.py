#!/usr/bin/env python
#
"""
Where credentials are kept, and how they are reached.

A secret store is named by a URL whose scheme picks the backend, so
nothing above this module knows which one is in use.

    op://<vault>/<item>
        A 1Password item.  Fields are read with `op read` and written
        with `op item edit`, so which `op` runs decides which account is
        reachable -- see TRIPSY_OP_BIN.

    hcvault://<host>/<mount>/<path>
        A HashiCorp Vault secret.  The host is the Vault server, falling
        back to VAULT_ADDR when the URL omits it; the first path segment
        is the secret engine's mount point and the rest is the path
        under it.  Always KV version 2, so the HTTP path carries `data/`
        between the mount and the path and a read unwraps `data.data`.
        Not implemented: there is no Vault to check it against, and this
        project observes rather than guesses.

1Password is the only backend implemented; the scheme is the seam a
second one arrives at.

Two capabilities, deliberately separate.  Every store can read a field.
A store that can also write one caches the API token beside the password,
which removes a `POST /auth` from every run and matters most for the
unattended ones.  A store that cannot write is not a lesser store -- it
simply fetches a fresh token each run, which is what the interactive path
does too.

A cached token is exactly as powerful as the password that produced it,
so it goes back into the same store rather than to a file beside the
archive.  That is the whole reason caching is a property of the store
rather than something this module does with a dot-file.
"""

# system imports
import os
import subprocess
from typing import Protocol
from urllib.parse import urlparse

# Names the secret store.  The scheme picks the backend.
#
SECRET_URL_ENV = "TRIPSY_SECRET_URL"

# Names the `op` binary.  More than one can be on a PATH and only the one
# the 1Password desktop app authorised can reach an account, so which one
# runs is worth being able to say.
#
OP_BIN_ENV = "TRIPSY_OP_BIN"

# Reserved: the scheme a Vault backend arrives under.  Named now because
# a URL someone writes today should still mean the same thing when the
# backend exists, and `vault` alone is too general a word to claim.
#
VAULT_SCHEME = "hcvault"

# The fields a store is asked for.  `token` is the only one written.
#
USERNAME = "username"
PASSWORD = "password"
TOKEN = "token"


########################################################################
########################################################################
#
class SecretError(Exception):
    """A store could not be reached, or would not answer."""


########################################################################
########################################################################
#
class SecretStore(Protocol):
    """Reads, and sometimes writes, the fields of one secret record."""

    # Whether `put` does anything.  A read-only store still works; the
    # token simply is not cached.
    #
    writable: bool

    ####################################################################
    #
    def get(self, field: str) -> str | None:
        """
        Read one field.

        Args:
            field: The field name, such as `username`.

        Returns:
            The value, or None when the record carries no such field.

        Raises:
            SecretError: The store itself could not be reached.
        """
        ...

    ####################################################################
    #
    def put(self, field: str, value: str) -> None:
        """
        Write one field, creating it if the record lacks it.

        Args:
            field: The field name.
            value: The value to store.

        Raises:
            SecretError: The write failed, or the store cannot write.
        """
        ...


########################################################################
########################################################################
#
class OnePasswordStore:
    """One 1Password item, reached through the `op` command line."""

    writable = True

    ####################################################################
    #
    def __init__(self, url: str, binary: str | None = None) -> None:
        """
        Args:
            url: An `op://vault/item` URL naming the item, without a
                field.
            binary: The `op` executable.  Defaults to `TRIPSY_OP_BIN`,
                then to whichever `op` is first on the PATH.
        """
        self.url = url.rstrip("/")
        self.binary = binary or os.environ.get(OP_BIN_ENV, "op")

    ####################################################################
    #
    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run `op`, reporting its own words when it refuses."""
        try:
            return subprocess.run(
                [self.binary, *arguments],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise SecretError(
                f"{self.binary} is not on the PATH; set {OP_BIN_ENV} to the "
                f"1Password CLI the desktop app authorised"
            ) from exc

    ####################################################################
    #
    def get(self, field: str) -> str | None:
        """
        Read one field of the item.

        A field the item does not carry is not an error: a token that has
        never been cached is exactly this case, and it is the ordinary
        state of a fresh item.

        Args:
            field: The field name.

        Returns:
            The value, or None when the item has no such field.

        Raises:
            SecretError: `op` could not answer at all.
        """
        result = self._run("read", f"{self.url}/{field}")
        if result.returncode == 0:
            return result.stdout.strip()

        complaint = result.stderr.strip()
        if "isn't a field" in complaint or "not found" in complaint.lower():
            return None
        raise SecretError(
            f"{self.binary} read {self.url}/{field} failed: "
            f"{complaint or 'no output'}"
        )

    ####################################################################
    #
    def put(self, field: str, value: str) -> None:
        """
        Write one field of the item, adding it when it is absent.

        Args:
            field: The field name.
            value: The value to store.

        Raises:
            SecretError: `op` refused the edit.
        """
        # The type is stated so a token lands as a concealed field rather
        # than as plain text anyone can read off the item at a glance.
        #
        result = self._run(
            "item", "edit", self.url, f"{field}[password]={value}"
        )
        if result.returncode != 0:
            raise SecretError(
                f"{self.binary} item edit {self.url} failed: "
                f"{result.stderr.strip() or 'no output'}"
            )


####################################################################
#
def store_for(url: str | None = None) -> SecretStore | None:
    """
    Build the store a URL names.

    Args:
        url: A store URL, or None to read `TRIPSY_SECRET_URL`.

    Returns:
        The store, or None when no URL was given anywhere.

    Raises:
        SecretError: The URL names a scheme with no backend behind it.
    """
    url = url or os.environ.get(SECRET_URL_ENV)
    if not url:
        return None

    scheme = urlparse(url).scheme
    if scheme == "op":
        return OnePasswordStore(url)

    if scheme == VAULT_SCHEME:
        raise SecretError(
            f"{VAULT_SCHEME}:// is not implemented yet -- its grammar is "
            f"{VAULT_SCHEME}://<host>/<mount>/<path>, always KV v2"
        )

    raise SecretError(
        f"no secret backend for {scheme or 'a URL with no'}:// -- "
        f"{SECRET_URL_ENV} understands op:// (1Password)"
    )
