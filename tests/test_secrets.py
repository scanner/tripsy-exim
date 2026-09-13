#!/usr/bin/env python
#
"""
Test the secret store layer.

`op` is never actually run: what is asserted is which command would be
run, and what is made of what it answers.  A test that shelled out to a
real 1Password would need a real vault and would write to it.
"""

# system imports
import subprocess
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check
from pytest_mock import MockerFixture

# Project imports
from tripsy_exim.secrets import (
    OP_BIN_ENV,
    SECRET_URL_ENV,
    TOKEN,
    USERNAME,
    OnePasswordStore,
    SecretError,
    store_for,
)


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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        GIVEN: no URL given and none in the environment
        WHEN:  a store is asked for
        THEN:  None comes back

        Having no secret store is an ordinary way to run: credentials can
        come from a flag or the environment instead.
        """
        monkeypatch.delenv(SECRET_URL_ENV, raising=False)

        assert store_for() is None

    ####################################################################
    #
    def test_the_environment_names_the_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        GIVEN: TRIPSY_SECRET_URL set
        WHEN:  a store is asked for with no argument
        THEN:  the environment's URL is used
        """
        monkeypatch.setenv(SECRET_URL_ENV, "op://Work/Tripsy")

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
    def test_the_vault_scheme_is_reserved_and_says_its_shape(self) -> None:
        """
        GIVEN: an hcvault:// URL
        WHEN:  a store is built for it
        THEN:  it says the backend is not built, and what the URL means

        The scheme is claimed before it is implemented so that a URL
        written today still means the same thing when it works.
        """
        with pytest.raises(SecretError, match="not implemented") as raised:
            store_for("hcvault://vault.example/secret/tripsy-exim")

        check.is_in("<mount>/<path>", str(raised.value))


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
    def test_a_missing_field_is_not_an_error(
        self, mocker: MockerFixture
    ) -> None:
        """
        GIVEN: an item carrying no such field
        WHEN:  the field is read
        THEN:  None comes back rather than an exception

        A token that has never been cached is exactly this case, and it
        is the ordinary state of a fresh item.
        """
        mocker.patch(
            "tripsy_exim.secrets.subprocess.run",
            return_value=completed(
                returncode=1,
                stderr=(
                    "[ERROR] could not read secret "
                    "'op://Personal/Tripsy/token': item 'Personal/Tripsy' "
                    "does not have a field 'token'"
                ),
            ),
        )
        store = OnePasswordStore("op://Personal/Tripsy")

        assert store.get(TOKEN) is None

    ####################################################################
    #
    def test_a_store_that_cannot_be_reached_says_so(
        self, mocker: MockerFixture
    ) -> None:
        """
        GIVEN: op refusing for a reason that is not a missing field
        WHEN:  a field is read
        THEN:  SecretError carries op's own words

        The failure that actually happens is an unauthorised binary, and
        a generic message would send someone hunting the wrong problem.
        """
        mocker.patch(
            "tripsy_exim.secrets.subprocess.run",
            return_value=completed(
                returncode=1,
                stderr="No accounts configured for use with 1Password CLI.",
            ),
        )
        store = OnePasswordStore("op://Personal/Tripsy")

        with pytest.raises(SecretError, match="No accounts configured"):
            store.get(USERNAME)

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

        store.put(TOKEN, "abc123")

        check.equal(
            run.call_args.args[0],
            [
                "op",
                "item",
                "edit",
                "Tripsy",
                "--vault",
                "Personal",
                "token[password]=abc123",
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
            store.put(TOKEN, "abc123")

    ####################################################################
    #
    def test_the_binary_comes_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
    ) -> None:
        """
        GIVEN: TRIPSY_OP_BIN naming a particular op
        WHEN:  a store is built without an explicit binary
        THEN:  that one is run

        More than one op can be on a PATH and only the one the desktop
        app authorised can reach an account.
        """
        monkeypatch.setenv(OP_BIN_ENV, "/usr/local/bin/op")
        run = mocker.patch(
            "tripsy_exim.secrets.subprocess.run",
            return_value=completed(stdout="value"),
        )

        OnePasswordStore("op://Personal/Tripsy").get(USERNAME)

        check.equal(run.call_args.args[0][0], "/usr/local/bin/op")


########################################################################
########################################################################
#
class TestProtocol:
    """Tests that the backend satisfies what callers are promised."""

    ####################################################################
    #
    @pytest.mark.parametrize("member", ["get", "put", "writable"])
    def test_the_backend_carries_every_member(self, member: str) -> None:
        """
        GIVEN: the 1Password backend
        WHEN:  the protocol's members are looked for
        THEN:  each is present

        A second backend is the whole point of the protocol, so what it
        has to provide is worth stating in a test rather than only in a
        type annotation.
        """
        store: Any = OnePasswordStore("op://Personal/Tripsy")

        assert hasattr(store, member)

    ####################################################################
    #
    @pytest.mark.parametrize(
        "complaint,missing",
        [
            pytest.param(
                "item 'Personal/Tripsy' does not have a field 'token'",
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
        THEN:  only an absent field comes back as None

        Matching too widely would swallow a broken store as an empty
        field and report a missing credential rather than the real fault
        -- which is what an unauthorised binary looks like.
        """
        mocker.patch(
            "tripsy_exim.secrets.subprocess.run",
            return_value=completed(returncode=1, stderr=complaint),
        )
        store = OnePasswordStore("op://Personal/Tripsy")

        if missing:
            assert store.get(TOKEN) is None
        else:
            with pytest.raises(SecretError):
                store.get(TOKEN)

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
