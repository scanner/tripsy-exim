#!/usr/bin/env python
#
"""
Test how a run finds its Tripsy token, username and password.

Two rules, and these tests are laid out to read as them:

- The token: a stored one is used; otherwise the run logs in and saves
  the new token to the store, if there is one; a refused stored token is
  replaced by logging in once more.
- The username and password: each from the first of flag, environment,
  store, prompt.
"""

# system imports
from collections.abc import MutableMapping
from typing import Any

# 3rd party imports
import click
import pytest
import pytest_check as check
from click.testing import CliRunner
from pytest_mock import MockerFixture

# Project imports
from tripsy_exim.api import TripsyClient
from tripsy_exim.api.errors import AuthenticationError, BadRequest
from tripsy_exim.cli import main, open_session, resolve_credentials
from tripsy_exim.secrets import PASSWORD, TOKEN, USERNAME, SecretError


########################################################################
########################################################################
#
class MemoryStore:
    """A secret store held in a dict, standing in for a real backend."""

    url = "memory://tripsy"

    ####################################################################
    #
    def __init__(self, **fields: str) -> None:
        self.fields = dict(fields)
        self.refuse_writes = False

    ####################################################################
    #
    def get(self, field: str) -> str | None:
        """Read one field."""
        return self.fields.get(field)

    ####################################################################
    #
    def put(self, field: str, value: str) -> None:
        """Write one field, unless told to refuse."""
        if self.refuse_writes:
            raise SecretError("store is read-only today")
        self.fields[field] = value


####################################################################
#
@pytest.fixture
def store(mocker: MockerFixture) -> MemoryStore:
    """An empty store, installed as the one TRIPSY_SECRET_URL names."""
    memory = MemoryStore()
    mocker.patch("tripsy_exim.cli.store_for", return_value=memory)
    return memory


####################################################################
#
@pytest.fixture
def no_store(mocker: MockerFixture) -> None:
    """No TRIPSY_SECRET_URL configured."""
    mocker.patch("tripsy_exim.cli.store_for", return_value=None)


####################################################################
#
@pytest.fixture
def tripsy_login(mocker: MockerFixture) -> Any:
    """
    `POST /auth`, answering with a new token each time it is called.

    Returns the mock, so a test can count logins and see who logged in.
    """
    tokens = iter(f"new-token-{n}" for n in range(1, 10))
    return mocker.patch.object(
        TripsyClient,
        "login",
        autospec=True,
        side_effect=lambda *a: next(tokens),
    )


########################################################################
########################################################################
#
class TestUsernameAndPassword:
    """Each of the two comes from the first place that has it."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "flags,env,stored,expected",
        [
            pytest.param(
                ("flag-user", "flag-pass"),
                ("env-user", "env-pass"),
                {USERNAME: "store-user", PASSWORD: "store-pass"},
                ("flag-user", "flag-pass"),
                id="1-flags-first",
            ),
            pytest.param(
                (None, None),
                ("env-user", "env-pass"),
                {USERNAME: "store-user", PASSWORD: "store-pass"},
                ("env-user", "env-pass"),
                id="2-then-environment",
            ),
            pytest.param(
                (None, None),
                (None, None),
                {USERNAME: "store-user", PASSWORD: "store-pass"},
                ("store-user", "store-pass"),
                id="3-then-store",
            ),
            pytest.param(
                ("flag-user", None),
                (None, None),
                {PASSWORD: "store-pass"},
                ("flag-user", "store-pass"),
                id="each-field-on-its-own",
            ),
        ],
    )
    def test_the_first_place_that_has_a_value_wins(
        self,
        environment: MutableMapping[str, str],
        flags: tuple[str | None, str | None],
        env: tuple[str | None, str | None],
        stored: dict[str, str],
        expected: tuple[str, str],
    ) -> None:
        """
        GIVEN: a username and password available from several places
        WHEN:  the credentials are resolved
        THEN:  each field is taken from the first of flag, environment,
               store
        """
        for name, value in zip(
            ("TRIPSY_USERNAME", "TRIPSY_PASSWORD"), env, strict=True
        ):
            if value is not None:
                environment[name] = value

        found = resolve_credentials(*flags, MemoryStore(**stored))

        assert found == expected

    ####################################################################
    #
    @pytest.mark.parametrize(
        "store,expected,asked",
        [
            pytest.param(
                MemoryStore(username="u"),
                ("u", "typed-password"),
                1,
                id="store-has-username",
            ),
            pytest.param(
                None,
                ("typed-username", "typed-password"),
                2,
                id="no-store-at-all",
            ),
        ],
    )
    def test_4_a_terminal_is_asked_only_for_what_is_missing(
        self,
        prompting: Any,
        store: MemoryStore | None,
        expected: tuple[str, str],
        asked: int,
    ) -> None:
        """
        GIVEN: a store holding only the username, or no store at all, at
               a terminal
        WHEN:  the credentials are resolved
        THEN:  only what is missing is asked for, the password hidden
        """
        found = resolve_credentials(None, None, store)

        check.equal(found, expected)
        check.equal(prompting.call_count, asked, "asked for what is missing")
        check.is_true(
            prompting.call_args.kwargs.get("hide_input"), "password hidden"
        )

    ####################################################################
    #
    def test_with_nowhere_left_to_look_the_error_lists_every_place(
        self,
    ) -> None:
        """
        GIVEN: no flag, no environment, an empty store, and no terminal
        WHEN:  the credentials are resolved
        THEN:  it fails, naming each place a value could have come from
        """
        with pytest.raises(click.ClickException) as raised:
            resolve_credentials(None, None, MemoryStore())

        for place in (
            "--username/--password",
            "TRIPSY_USERNAME",
            "TRIPSY_SECRET_URL",
            "terminal",
        ):
            check.is_in(place, raised.value.message)


########################################################################
########################################################################
#
class TestToken:
    """Where the token a run authenticates with comes from."""

    ####################################################################
    #
    def test_1_a_stored_token_is_used_without_logging_in(
        self, store: MemoryStore, tripsy_login: Any
    ) -> None:
        """
        GIVEN: a store holding a token and nothing else
        WHEN:  a session is opened
        THEN:  the token is used, and no username or password is needed
        """
        store.fields[TOKEN] = "stored-token"

        with open_session(None, None) as client:
            check.equal(client.auth.token, "stored-token")
        check.equal(tripsy_login.call_count, 0, "never logged in")

    ####################################################################
    #
    def test_2_without_one_the_run_logs_in_and_saves_the_token(
        self,
        store: MemoryStore,
        tripsy_login: Any,
        prompting: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """
        GIVEN: a store holding neither token nor password, at a terminal
        WHEN:  a session is opened
        THEN:  the username and password are asked for, the new token is
               saved to the store and not the password, and the run says
               where the token went
        """
        with open_session(None, None) as client:
            check.equal(client.auth.token, "new-token-1")

        check.equal(store.fields, {TOKEN: "new-token-1"}, "only the token")
        check.is_in("token saved to memory://tripsy", capsys.readouterr().err)

    ####################################################################
    #
    def test_2_with_no_store_the_token_is_not_saved_and_it_says_so(
        self,
        no_store: None,
        tripsy_login: Any,
        environment: MutableMapping[str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """
        GIVEN: credentials in the environment and no store
        WHEN:  a session is opened
        THEN:  the run logs in and says the token is not being saved
        """
        environment["TRIPSY_USERNAME"] = "u"
        environment["TRIPSY_PASSWORD"] = "p"

        with open_session(None, None) as client:
            check.equal(client.auth.token, "new-token-1")

        check.is_in("not saved", capsys.readouterr().err)

    ####################################################################
    #
    def test_3_a_refused_stored_token_is_replaced(
        self, store: MemoryStore, tripsy_login: Any, prompting: Any
    ) -> None:
        """
        GIVEN: a stored token Tripsy no longer accepts
        WHEN:  the client re-authenticates, as it does on a 401
        THEN:  the run logs in again and the new token replaces the old
        """
        store.fields[TOKEN] = "spent-token"

        with open_session(None, None) as client:
            assert client.reauthenticate is not None
            replacement = client.reauthenticate()

        check.equal(replacement, "new-token-1")
        check.equal(store.fields[TOKEN], "new-token-1")

    ####################################################################
    #
    def test_a_token_that_cannot_be_saved_does_not_stop_the_run(
        self,
        store: MemoryStore,
        tripsy_login: Any,
        prompting: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """
        GIVEN: a store that refuses the write
        WHEN:  a session is opened
        THEN:  the run carries on with its token and says the save failed
        """
        store.refuse_writes = True

        with open_session(None, None) as client:
            check.equal(client.auth.token, "new-token-1")

        check.is_in("could not save the token", capsys.readouterr().err)

    ####################################################################
    #
    def test_a_refused_password_is_a_plain_error(
        self, store: MemoryStore, prompting: Any, mocker: MockerFixture
    ) -> None:
        """
        GIVEN: a username and password Tripsy rejects
        WHEN:  a session is opened
        THEN:  it fails saying so, without saving anything
        """
        mocker.patch.object(
            TripsyClient,
            "login",
            side_effect=BadRequest(
                "bad", status_code=400, method="POST", url="/auth"
            ),
        )

        with pytest.raises(click.ClickException, match="refused"):
            with open_session(None, None):
                pass

        check.equal(store.fields, {}, "nothing saved")


########################################################################
########################################################################
#
class TestAuthCheck:
    """
    Tests for `auth check`, which authenticates the way every command
    does and says how.
    """

    ####################################################################
    #
    @pytest.fixture
    def account(self, mocker: MockerFixture) -> Any:
        """
        The request `auth check` makes, answering with one trip id.

        Returns the mock, so a test can make Tripsy refuse it instead.
        """
        return mocker.patch.object(
            TripsyClient,
            "iter_trips",
            autospec=True,
            side_effect=lambda *a, **k: iter([{"id": 1}]),
        )

    ####################################################################
    #
    def test_a_stored_token_is_named(
        self,
        runner: CliRunner,
        store: MemoryStore,
        account: Any,
        tripsy_login: Any,
    ) -> None:
        """
        GIVEN: a store holding a token
        WHEN:  auth check is run
        THEN:  it says which store the token came from, that Tripsy
               accepted it, and exits 0 without logging in
        """
        store.fields[TOKEN] = "stored-token"

        result = runner.invoke(main, ["auth", "check"])

        check.equal(result.exit_code, 0, result.output)
        check.is_in("using the token stored in memory://tripsy", result.output)
        check.is_in("Tripsy accepted the token", result.output)
        check.equal(tripsy_login.call_count, 0, "never logged in")

    ####################################################################
    #
    def test_without_a_token_it_logs_in_and_saves_one(
        self,
        runner: CliRunner,
        store: MemoryStore,
        account: Any,
        tripsy_login: Any,
        environment: MutableMapping[str, str],
    ) -> None:
        """
        GIVEN: an empty store and credentials in the environment
        WHEN:  auth check is run
        THEN:  it logs in, saves the token, says so, and exits 0
        """
        environment["TRIPSY_USERNAME"] = "u"
        environment["TRIPSY_PASSWORD"] = "p"

        result = runner.invoke(main, ["auth", "check"])

        check.equal(result.exit_code, 0, result.output)
        check.is_in("token saved to memory://tripsy", result.output)
        check.equal(store.fields[TOKEN], "new-token-1")

    ####################################################################
    #
    def test_a_refusal_exits_1(
        self,
        runner: CliRunner,
        store: MemoryStore,
        account: Any,
        mocker: MockerFixture,
    ) -> None:
        """
        GIVEN: a stored token Tripsy refuses, and no way to log in again
        WHEN:  auth check is run
        THEN:  it exits 1 with an error, never claiming success
        """
        store.fields[TOKEN] = "spent-token"
        account.side_effect = AuthenticationError(
            "refused", status_code=401, method="GET", url="/v2/trips"
        )

        result = runner.invoke(main, ["auth", "check"])

        check.equal(result.exit_code, 1, result.output)
        check.is_not_in("accepted", result.output)
