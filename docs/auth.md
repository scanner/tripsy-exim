# auth(1) -- check how this machine authenticates to Tripsy

## SYNOPSIS

```text
tripsy-exim auth check [--username TEXT] [--password TEXT]
```

## RUNNING

Examples below are written as `uv run tripsy-exim`, which is how the
command runs from a fresh clone. See [the docs README](README.md) for
when you can drop the `uv run`.

## DESCRIPTION

`auth check` authenticates exactly as every other command does, makes
one small request -- the first page of trip ids -- and says how it got
its token:

```text
using the token stored in hcvault://vault.example.com/secret/tripsy
Tripsy accepted the token
```

or, when there was no stored token and it had to log in:

```text
logged in to Tripsy as you@example.com; token saved to hcvault://vault.example.com/secret/tripsy
Tripsy accepted the token
```

It follows the rules in [CREDENTIALS in the README](../README.md#credentials):
a stored token is used first, a refused one is replaced by logging in
again, and a login takes the username and password from the flags, the
environment, the store, or a prompt at a terminal. So it is the command
to run after setting up a store, and before a scheduled run depends on
one: a spent token is found and replaced here rather than partway
through an export.

Nothing is written to Tripsy.

## OPTIONS

`--username TEXT`, `--password TEXT`
: Credentials for a login, if one is needed. A stored token is used in
  preference to them.

## EXIT STATUS

`0`
: Tripsy accepted the token.

`1`
: It did not, or no username and password could be found for a login.
  The message says which.

## EXAMPLES

Set up a store that holds only the token, typing the password once:

```sh
export TRIPSY_SECRET_URL="hcvault:///secret/tripsy"
uv run tripsy-exim auth check        # asks for username and password
uv run tripsy-exim auth check        # uses the saved token
```

## SEE ALSO

[list(1)](list.md), [export(1)](export.md)
