#!/usr/bin/env python
#
"""
Test the secret store layer.

`op` is never actually run, and Vault is never actually reached: what is
asserted is which command or request would be sent, and what is made of
the answer.  A test that reached a real store would write to it.
"""

# system imports
import json
import re
import subprocess
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

# 3rd party imports
import httpx
import pytest
import pytest_check as check
from pytest_mock import MockerFixture

# Project imports
from tripsy_exim import secrets
from tripsy_exim.secrets import (
    OP_BIN_ENV,
    SECRET_URL_ENV,
    TOKEN,
    USERNAME,
    VAULT_ADDR_ENV,
    VAULT_TOKEN_ENV,
    HashiCorpVaultStore,
    OnePasswordStore,
    SecretError,
    store_for,
)

# Invented, and only ever sent to a mock transport.
#
VAULT_TOKEN = "hvs.not-a-real-vault-token"


####################################################################
#
def completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """One finished `op` invocation."""
    return subprocess.CompletedProcess(
        args=["op"], returncode=returncode, stdout=stdout, stderr=stderr
    )


########################################################################
########################################################################
#
class TestStoreFor:
    """Tests for picking a backend from a URL."""

    ####################################################################
    #
    def test_an_op_url_builds_a_1password_store(self) -> None:
        """
        GIVEN: an op:// URL
        WHEN:  a store is built for it
        THEN:  it is the 1Password backend, holding that URL
        """
        store = store_for("op://Personal/Tripsy")

        check.is_instance(store, OnePasswordStore)
        check.equal(getattr(store, "url", None), "op://Personal/Tripsy")

    ####################################################################
    #
    def test_no_url_anywhere_is_not_an_error(
        self, environment: MutableMapping[str, str]
    ) -> None:
        """
        GIVEN: no URL given and none in the environment
        WHEN:  a store is asked for
        THEN:  None comes back

        Having no secret store is an ordinary way to run: credentials can
        come from a flag or the environment instead.
        """
        environment.pop(SECRET_URL_ENV, None)

        assert store_for() is None

    ####################################################################
    #
    def test_the_environment_names_the_store(
        self, environment: MutableMapping[str, str]
    ) -> None:
        """
        GIVEN: TRIPSY_SECRET_URL set
        WHEN:  a store is asked for with no argument
        THEN:  the environment's URL is used
        """
        environment[SECRET_URL_ENV] = "op://Work/Tripsy"

        store = store_for()

        check.equal(getattr(store, "url", None), "op://Work/Tripsy")

    ####################################################################
    #
    def test_a_scheme_with_no_backend_is_refused(self) -> None:
        """
        GIVEN: a URL naming a backend that does not exist
        WHEN:  a store is built for it
        THEN:  it says so, naming what it does understand

        Falling back to a default backend would read the wrong store and
        report a missing credential rather than a misconfiguration.
        """
        with pytest.raises(SecretError, match="op://"):
            store_for("file:///tmp/secrets")

    ####################################################################
    #
    def test_an_hcvault_url_builds_a_vault_store(self) -> None:
        """
        GIVEN: an hcvault:// URL
        WHEN:  a store is built for it
        THEN:  it is the HashiCorp Vault backend, holding that URL
        """
        store = store_for("hcvault://vault.example/secret/tripsy-exim")

        check.is_instance(store, HashiCorpVaultStore)
        check.equal(
            getattr(store, "url", None),
            "hcvault://vault.example/secret/tripsy-exim",
        )


########################################################################
########################################################################
#
class TestOnePasswordStore:
    """Tests for the 1Password backend."""

    ####################################################################
    #
    def test_a_field_is_read_through_op(self, mocker: MockerFixture) -> None:
        """
        GIVEN: an item and a field
        WHEN:  the field is read
        THEN:  `op read` is run against that field, and its output used
        """
        run = mocker.patch(
            "tripsy_exim.secrets.subprocess.run",
            return_value=completed(stdout="scanner@example.invalid\n"),
        )
        store = OnePasswordStore("op://Personal/Tripsy", binary="/usr/bin/op")

        value = store.get(USERNAME)

        check.equal(value, "scanner@example.invalid", "whitespace stripped")
        check.equal(
            run.call_args.args[0],
            ["/usr/bin/op", "read", "op://Personal/Tripsy/username"],
        )

    ####################################################################
    #
    @pytest.mark.parametrize(
        "complaint,missing",
        [
            pytest.param(
                "[ERROR] could not read secret "
                "'op://Personal/Tripsy/token': item 'Personal/Tripsy' "
                "does not have a field 'token'",
                True,
                id="op-2.32-wording",
            ),
            pytest.param(
                '"token" isn\'t a field in the item',
                True,
                id="older-wording",
            ),
            pytest.param(
                "No accounts configured for use with 1Password CLI.",
                False,
                id="unauthorised-binary",
            ),
            pytest.param(
                "could not connect to 1Password desktop app",
                False,
                id="app-not-running",
            ),
        ],
    )
    def test_only_an_absent_field_reads_as_absent(
        self, mocker: MockerFixture, complaint: str, missing: bool
    ) -> None:
        """
        GIVEN: op refusing with a particular complaint
        WHEN:  a field is read
        THEN:  an absent field comes back as None, and anything else
               raises carrying op's own words

        A token that has never been cached is an absent field, and it is
        the ordinary state of a fresh item.  Matching too widely would
        swallow a broken store as an empty field and report a missing
        credential rather than the real fault -- which is what an
        unauthorised binary looks like, and a generic message would send
        someone hunting the wrong problem.
        """
        mocker.patch(
            "tripsy_exim.secrets.subprocess.run",
            return_value=completed(returncode=1, stderr=complaint),
        )
        store = OnePasswordStore("op://Personal/Tripsy")

        if missing:
            assert store.get(TOKEN) is None
        else:
            with pytest.raises(SecretError, match=re.escape(complaint)):
                store.get(TOKEN)

    ####################################################################
    #
    def test_a_missing_binary_names_the_override(
        self, mocker: MockerFixture
    ) -> None:
        """
        GIVEN: no such executable
        WHEN:  a field is read
        THEN:  the error names the variable that points at the right one
        """
        mocker.patch(
            "tripsy_exim.secrets.subprocess.run",
            side_effect=FileNotFoundError("nope"),
        )
        store = OnePasswordStore("op://Personal/Tripsy", binary="op-missing")

        with pytest.raises(SecretError, match=OP_BIN_ENV):
            store.get(USERNAME)

    ####################################################################
    #
    def test_a_token_is_written_as_a_concealed_field(
        self, mocker: MockerFixture
    ) -> None:
        """
        GIVEN: a token to cache
        WHEN:  it is written
        THEN:  `op item edit` stores it as a password-typed field

        A token is as powerful as the password beside it, so it should
        not be the one field on the item anyone can read at a glance.
        """
        run = mocker.patch(
            "tripsy_exim.secrets.subprocess.run", return_value=completed()
        )
        store = OnePasswordStore("op://Personal/Tripsy", binary="op")

        store.put(TOKEN, "not-a-real-token")

        check.equal(
            run.call_args.args[0],
            [
                "op",
                "item",
                "edit",
                "Tripsy",
                "--vault",
                "Personal",
                "token[password]=not-a-real-token",
            ],
        )

    ####################################################################
    #
    def test_a_refused_write_is_reported(self, mocker: MockerFixture) -> None:
        """
        GIVEN: op refusing the edit
        WHEN:  a token is written
        THEN:  SecretError carries what op said
        """
        mocker.patch(
            "tripsy_exim.secrets.subprocess.run",
            return_value=completed(returncode=1, stderr="read-only item"),
        )
        store = OnePasswordStore("op://Personal/Tripsy")

        with pytest.raises(SecretError, match="read-only item"):
            store.put(TOKEN, "not-a-real-token")

    ####################################################################
    #
    def test_the_binary_comes_from_the_environment(
        self, environment: MutableMapping[str, str], mocker: MockerFixture
    ) -> None:
        """
        GIVEN: TRIPSY_OP_BIN naming a particular op
        WHEN:  a store is built without an explicit binary
        THEN:  that one is run

        More than one op can be on a PATH and only the one the desktop
        app authorised can reach an account.
        """
        environment[OP_BIN_ENV] = "/usr/local/bin/op"
        run = mocker.patch(
            "tripsy_exim.secrets.subprocess.run",
            return_value=completed(stdout="value"),
        )

        OnePasswordStore("op://Personal/Tripsy").get(USERNAME)

        check.equal(run.call_args.args[0][0], "/usr/local/bin/op")


########################################################################
########################################################################
#
class TestHashiCorpVaultStore:
    """Tests for the HashiCorp Vault backend."""

    ####################################################################
    #
    @pytest.fixture
    def vault(self) -> Callable[..., tuple[HashiCorpVaultStore, list[Any]]]:
        """
        A Vault store talking to a scripted server.

        Call it with the responses the server should give, in order, as
        (status, body) pairs.  Returns the store and the list the server
        records each request into.
        """

        def build(
            *answers: tuple[int, dict[str, Any]],
        ) -> tuple[HashiCorpVaultStore, list[httpx.Request]]:
            seen: list[httpx.Request] = []
            queue = list(answers)

            def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request)
                status, body = queue.pop(0)
                return httpx.Response(status, json=body)

            store = HashiCorpVaultStore(
                "hcvault://vault.example:8200/secret/apps/tripsy",
                transport=httpx.MockTransport(handler),
            )
            return store, seen

        return build

    ####################################################################
    #
    @pytest.fixture
    def vault_token(self, environment: MutableMapping[str, str]) -> str:
        """A Vault token in VAULT_TOKEN."""
        environment[VAULT_TOKEN_ENV] = VAULT_TOKEN
        return VAULT_TOKEN

    ####################################################################
    #
    @pytest.mark.parametrize(
        "url,vault_addr,address,mount,path",
        [
            pytest.param(
                "hcvault://vault.example:8200/secret/apps/tripsy",
                None,
                "https://vault.example:8200",
                "secret",
                "apps/tripsy",
                id="host-in-url",
            ),
            pytest.param(
                "hcvault:///kv/tripsy",
                "https://from-env.example:8200/",
                "https://from-env.example:8200",
                "kv",
                "tripsy",
                id="host-from-VAULT_ADDR",
            ),
        ],
    )
    def test_the_url_names_the_server_the_mount_and_the_path(
        self,
        environment: MutableMapping[str, str],
        url: str,
        vault_addr: str | None,
        address: str,
        mount: str,
        path: str,
    ) -> None:
        """
        GIVEN: an hcvault:// URL, with or without a host
        WHEN:  a store is built
        THEN:  the server is the URL's host over https, or VAULT_ADDR;
               the first path segment is the mount and the rest the path
        """
        if vault_addr is not None:
            environment[VAULT_ADDR_ENV] = vault_addr

        store = HashiCorpVaultStore(url)

        check.equal(store.address, address)
        check.equal(store.mount, mount)
        check.equal(store.path, path)

    ####################################################################
    #
    @pytest.mark.parametrize(
        "url,complaint",
        [
            pytest.param(
                "hcvault:///kv/tripsy", VAULT_ADDR_ENV, id="no-server"
            ),
            pytest.param(
                "hcvault://vault.example/kv", "<mount>/<path>", id="no-path"
            ),
        ],
    )
    def test_a_url_that_names_nothing_usable_is_refused(
        self, url: str, complaint: str
    ) -> None:
        """
        GIVEN: a URL with no server anywhere, or no path under the mount
        WHEN:  a store is built
        THEN:  it is refused, saying what is missing
        """
        with pytest.raises(SecretError, match=re.escape(complaint)):
            HashiCorpVaultStore(url)

    ####################################################################
    #
    @pytest.mark.parametrize(
        "in_env,in_file,expected",
        [
            pytest.param("from-env", "from-file", "from-env", id="env-first"),
            pytest.param(None, "from-file\n", "from-file", id="then-file"),
        ],
    )
    def test_the_vault_token_is_found_as_the_vault_cli_finds_it(
        self,
        environment: MutableMapping[str, str],
        vault: Callable[..., tuple[HashiCorpVaultStore, list[Any]]],
        in_env: str | None,
        in_file: str | None,
        expected: str,
    ) -> None:
        """
        GIVEN: a Vault token in VAULT_TOKEN, ~/.vault-token, or both
        WHEN:  a field is read
        THEN:  VAULT_TOKEN is sent if set, otherwise the file's
        """
        if in_env is not None:
            environment[VAULT_TOKEN_ENV] = in_env
        if in_file is not None:
            Path(secrets.VAULT_TOKEN_FILE).write_text(in_file)
        store, seen = vault((404, {"errors": []}))

        store.get(TOKEN)

        assert seen[0].headers["X-Vault-Token"] == expected

    ####################################################################
    #
    def test_no_vault_token_anywhere_says_where_to_put_one(
        self, vault: Callable[..., tuple[HashiCorpVaultStore, list[Any]]]
    ) -> None:
        """
        GIVEN: no VAULT_TOKEN and no ~/.vault-token
        WHEN:  a field is read
        THEN:  it fails naming both places
        """
        store, _ = vault()

        with pytest.raises(SecretError) as raised:
            store.get(TOKEN)

        check.is_in(VAULT_TOKEN_ENV, str(raised.value))
        check.is_in("vault login", str(raised.value))

    ####################################################################
    #
    @pytest.mark.parametrize(
        "status,body,expected",
        [
            pytest.param(
                200,
                {"data": {"data": {"token": "t", "username": "u"}}},
                "t",
                id="present",
            ),
            pytest.param(
                200, {"data": {"data": {"username": "u"}}}, None, id="no-field"
            ),
            pytest.param(404, {"errors": []}, None, id="no-secret-yet"),
        ],
    )
    def test_a_field_is_read_from_the_kv2_data_path(
        self,
        vault_token: str,
        vault: Callable[..., tuple[HashiCorpVaultStore, list[Any]]],
        status: int,
        body: dict[str, Any],
        expected: str | None,
    ) -> None:
        """
        GIVEN: a secret holding the field, lacking it, or not existing yet
        WHEN:  the field is read
        THEN:  the value comes back, or None for either kind of absence
        """
        store, seen = vault((status, body))

        value = store.get(TOKEN)

        check.equal(value, expected)
        check.equal(seen[0].method, "GET")
        check.equal(seen[0].url.path, "/v1/secret/data/apps/tripsy")

    ####################################################################
    #
    def test_a_refused_read_carries_what_vault_said(
        self,
        vault_token: str,
        vault: Callable[..., tuple[HashiCorpVaultStore, list[Any]]],
    ) -> None:
        """
        GIVEN: a Vault token without permission on the path
        WHEN:  a field is read
        THEN:  SecretError carries Vault's own words and not the token
        """
        store, _ = vault((403, {"errors": ["permission denied"]}))

        with pytest.raises(SecretError, match="permission denied") as raised:
            store.get(TOKEN)

        check.is_not_in(VAULT_TOKEN, str(raised.value))

    ####################################################################
    #
    def test_a_write_patches_only_its_own_field(
        self,
        vault_token: str,
        vault: Callable[..., tuple[HashiCorpVaultStore, list[Any]]],
    ) -> None:
        """
        GIVEN: a secret that already exists
        WHEN:  the token is written
        THEN:  one merge patch carries the token and nothing else, so any
               other field on the secret is left alone
        """
        store, seen = vault((200, {"data": {}}))

        store.put(TOKEN, "new-token")

        check.equal(len(seen), 1)
        check.equal(seen[0].method, "PATCH")
        check.equal(
            seen[0].headers["Content-Type"], "application/merge-patch+json"
        )
        check.equal(json.loads(seen[0].content), {"data": {TOKEN: "new-token"}})

    ####################################################################
    #
    def test_the_first_write_creates_the_secret_only_if_still_absent(
        self,
        vault_token: str,
        vault: Callable[..., tuple[HashiCorpVaultStore, list[Any]]],
    ) -> None:
        """
        GIVEN: a secret that does not exist yet, so a patch is refused
        WHEN:  the token is written
        THEN:  the secret is created with check-and-set 0, which Vault
               refuses if someone created it in the meantime
        """
        store, seen = vault((404, {"errors": []}), (200, {"data": {}}))

        store.put(TOKEN, "new-token")

        check.equal([r.method for r in seen], ["PATCH", "POST"])
        check.equal(
            json.loads(seen[1].content),
            {"options": {"cas": 0}, "data": {TOKEN: "new-token"}},
        )

    ####################################################################
    #
    def test_a_refused_write_carries_what_vault_said(
        self,
        vault_token: str,
        vault: Callable[..., tuple[HashiCorpVaultStore, list[Any]]],
    ) -> None:
        """
        GIVEN: a policy without patch permission
        WHEN:  the token is written
        THEN:  SecretError carries Vault's own words
        """
        store, _ = vault(
            (403, {"errors": ["1 error occurred: permission denied"]})
        )

        with pytest.raises(SecretError, match="permission denied"):
            store.put(TOKEN, "new-token")


########################################################################
########################################################################
#
class TestProtocol:
    """Tests that the backend satisfies what callers are promised."""

    ####################################################################
    #
    @pytest.mark.parametrize("member", ["get", "put", "url"])
    @pytest.mark.parametrize(
        "url",
        ["op://Personal/Tripsy", "hcvault://vault.example/secret/tripsy"],
    )
    def test_every_backend_carries_every_member(
        self, url: str, member: str
    ) -> None:
        """
        GIVEN: each backend
        WHEN:  the protocol's members are looked for
        THEN:  each is present

        What a backend has to provide is worth stating in a test rather
        than only in a type annotation.
        """
        store: Any = store_for(url)

        assert hasattr(store, member)

    ####################################################################
    #
    def test_the_url_is_split_into_a_vault_and_an_item(self) -> None:
        """
        GIVEN: an op:// URL
        WHEN:  a store is built from it
        THEN:  the vault and item are available separately

        `op read` takes the URL but `op item edit` refuses it, wanting
        the item named on its own with its vault beside it.
        """
        store = OnePasswordStore("op://Personal/Tripsy")

        check.equal(store.vault, "Personal")
        check.equal(store.item, "Tripsy")
