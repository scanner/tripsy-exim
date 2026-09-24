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
        A HashiCorp Vault secret in a KV version 2 engine.  The host is
        the Vault server, reached over https, falling back to VAULT_ADDR
        when the URL omits it; the first path segment is the engine's
        mount point and the rest is the path under it.  The Vault token
        comes from VAULT_TOKEN, then ~/.vault-token -- whatever `vault
        login` left behind -- and VAULT_CACERT names a CA bundle, as for
        the `vault` command line.

Every store can read a field and write one.  The username and password
are only ever read; the API token is the one field written.  A store
need not hold the username and password at all: one that holds only the
token is how a password kept somewhere this project cannot reach still
gets its token cached.

A cached token is exactly as powerful as the password that produced it,
so it goes into a secret store rather than to a file beside the archive.
"""

# system imports
import os
import subprocess
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

# 3rd party imports
import httpx

# Names the secret store.  The scheme picks the backend.
#
SECRET_URL_ENV = "TRIPSY_SECRET_URL"

# Names the `op` binary.  More than one can be on a PATH and only the one
# the 1Password desktop app authorised can reach an account, so which one
# runs is worth being able to say.
#
OP_BIN_ENV = "TRIPSY_OP_BIN"

# The scheme a HashiCorp Vault store is named by.  `vault` alone is too
# general a word to claim.
#
VAULT_SCHEME = "hcvault"

# Where Vault's own command line keeps what it needs, read here the same
# way so that a `vault login` is all the setup a store requires.
#
VAULT_ADDR_ENV = "VAULT_ADDR"
VAULT_TOKEN_ENV = "VAULT_TOKEN"
VAULT_CACERT_ENV = "VAULT_CACERT"
VAULT_TOKEN_FILE = Path.home() / ".vault-token"

# Seconds before a request to Vault is abandoned.
#
VAULT_TIMEOUT = 15.0

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
    """Reads and writes the fields of one secret record."""

    # The URL the store was named by, for saying where a token went.
    #
    url: str

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

        # `op read` takes the URL, but `op item edit` does not: it wants
        # the item named on its own with its vault beside it.  Both halves
        # come from the one URL so a caller still configures one thing.
        #
        parsed = urlparse(self.url)
        self.vault = parsed.netloc
        self.item = parsed.path.strip("/")

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
        if _is_missing_field(complaint):
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
            "item",
            "edit",
            self.item,
            "--vault",
            self.vault,
            f"{field}[password]={value}",
        )
        if result.returncode != 0:
            raise SecretError(
                f"{self.binary} item edit {self.item} failed: "
                f"{result.stderr.strip() or 'no output'}"
            )


########################################################################
########################################################################
#
class HashiCorpVaultStore:
    """One secret in a HashiCorp Vault KV version 2 engine."""

    ####################################################################
    #
    def __init__(
        self, url: str, transport: httpx.BaseTransport | None = None
    ) -> None:
        """
        Args:
            url: An `hcvault://<host>/<mount>/<path>` URL.  An empty host
                -- `hcvault:///<mount>/<path>` -- means VAULT_ADDR.
            transport: The HTTP transport, for tests.

        Raises:
            SecretError: The URL names no mount and path, or no server
                can be found for it.
        """
        self.url = url.rstrip("/")
        parsed = urlparse(self.url)

        mount, _, path = parsed.path.strip("/").partition("/")
        if not mount or not path:
            raise SecretError(
                f"{self.url} names no secret: the form is "
                f"{VAULT_SCHEME}://<host>/<mount>/<path>"
            )
        self.mount = mount
        self.path = path

        if parsed.netloc:
            self.address = f"https://{parsed.netloc}"
        else:
            address = os.environ.get(VAULT_ADDR_ENV)
            if not address:
                raise SecretError(
                    f"{self.url} names no Vault server and "
                    f"{VAULT_ADDR_ENV} is not set"
                )
            self.address = address.rstrip("/")

        self._transport = transport

    ####################################################################
    #
    def _client(self) -> httpx.Client:
        """A client carrying the Vault token, built when a field is used."""
        options: dict[str, Any] = {
            "base_url": self.address,
            "headers": {"X-Vault-Token": _vault_token()},
            "timeout": VAULT_TIMEOUT,
        }
        if self._transport is not None:
            options["transport"] = self._transport
        cacert = os.environ.get(VAULT_CACERT_ENV)
        if cacert:
            options["verify"] = cacert
        return httpx.Client(**options)

    ####################################################################
    #
    @property
    def _endpoint(self) -> str:
        """The KV version 2 data path for this secret."""
        return f"/v1/{self.mount}/data/{self.path}"

    ####################################################################
    #
    def get(self, field: str) -> str | None:
        """
        Read one field of the secret.

        A secret that does not exist yet is not an error: before the first
        token is written, a token-only secret is exactly this.

        Args:
            field: The field name.

        Returns:
            The value, or None when the secret or the field is absent.

        Raises:
            SecretError: Vault could not be reached or refused the read.
        """
        response = self._request("GET")
        if response.status_code == 404:
            return None
        _raise_for(response, f"read {self.url}")

        data = ((response.json() or {}).get("data") or {}).get("data") or {}
        value = data.get(field)
        return None if value is None else str(value)

    ####################################################################
    #
    def put(self, field: str, value: str) -> None:
        """
        Write one field, leaving every other field of the secret alone.

        A merge patch touches only the named field.  Vault refuses to
        patch a secret that does not exist yet, so the first write creates
        it instead, with check-and-set 0 so that a secret created by
        someone else in the meantime is refused rather than overwritten.

        Observed 2026-09-24 against Vault 2.0.3: GET on an absent secret
        answers 404, PATCH on one answers 404 and the POST that follows
        200, and a later PATCH of one field leaves the others in place.

        Args:
            field: The field name.
            value: The value to store.

        Raises:
            SecretError: Vault could not be reached or refused the write.
        """
        response = self._request(
            "PATCH",
            json={"data": {field: value}},
            headers={"Content-Type": "application/merge-patch+json"},
        )
        if response.status_code == 404:
            response = self._request(
                "POST", json={"options": {"cas": 0}, "data": {field: value}}
            )
        _raise_for(response, f"write {field} to {self.url}")

    ####################################################################
    #
    def _request(self, method: str, **kwargs: Any) -> httpx.Response:
        """Send one request to this secret's data path."""
        try:
            with self._client() as client:
                return client.request(method, self._endpoint, **kwargs)
        except httpx.HTTPError as exc:
            raise SecretError(
                f"could not reach Vault at {self.address}: {exc}"
            ) from exc

    ####################################################################
    #
    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.url}>"


####################################################################
#
def _vault_token() -> str:
    """
    The token Vault is authenticated to, as its own command line finds it.

    Raises:
        SecretError: Neither VAULT_TOKEN nor ~/.vault-token has one.
    """
    token = os.environ.get(VAULT_TOKEN_ENV)
    if token:
        return token.strip()
    try:
        token = VAULT_TOKEN_FILE.read_text().strip()
    except OSError:
        token = ""
    if token:
        return token
    raise SecretError(
        f"no Vault token: set {VAULT_TOKEN_ENV} or run `vault login`, "
        f"which writes {VAULT_TOKEN_FILE}"
    )


####################################################################
#
def _raise_for(response: httpx.Response, action: str) -> None:
    """
    Raise when Vault refused, carrying what it said.

    Vault reports failures as a list of strings under `errors`.
    """
    if response.is_success:
        return
    try:
        errors = (response.json() or {}).get("errors") or []
    except ValueError:
        errors = []
    said = "; ".join(str(e) for e in errors) or response.reason_phrase
    raise SecretError(
        f"Vault would not {action}: {response.status_code} {said}"
    )


####################################################################
#
def _is_missing_field(complaint: str) -> bool:
    """
    Whether `op` refused because the field is not there.

    A field that does not exist is an ordinary answer -- a token that has
    never been cached is exactly this -- while anything else is a store
    that could not be reached.  Only `op` can tell them apart, and only
    in prose, so the wordings it uses are matched and nothing else is:
    guessing wide would swallow a real failure as an absent field and
    report a missing credential instead of a broken store.

    Observed 2026-09-12 against op 2.32:

        item 'Personal/Tripsy' does not have a field 'token'

    Args:
        complaint: What `op` wrote to standard error.

    Returns:
        Whether this names an absent field.
    """
    lowered = complaint.lower()
    return "field" in lowered and (
        "does not have a field" in lowered or "isn't a field" in lowered
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
        return HashiCorpVaultStore(url)

    raise SecretError(
        f"no secret backend for {scheme or 'a URL with no'}:// -- "
        f"{SECRET_URL_ENV} understands op:// (1Password) and "
        f"{VAULT_SCHEME}:// (HashiCorp Vault)"
    )
