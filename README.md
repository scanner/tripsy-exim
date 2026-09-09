# tripsy-exim

Export and import trip data for [Tripsy.app](https://tripsy.app).

## Why

TripIt disabled its third-party integrations on 2026-08-29, returning `404`
to any new connection attempt
([announcement](https://tripsy.blog/tripit-third-party-integrations-are-no-longer-working/)).
Existing connections reportedly still work, but a Tripsy account created
after that date can never link one -- so years of TripIt trip data have no
supported path into the service that replaced it.

That is the first half of this project: get the data in, from TripIt's GDPR
export or from the per-trip `.ics` files TripIt will still hand you.

The second half follows from the first. Being locked inside a service is
what created the problem, so `tripsy-exim` also keeps a complete, current,
local copy of your trips in a provider-neutral format. If you ever move
again, you move with your data.

## Status

Early. The project layout, tooling, and API research are in place; the
importers, the API client, and the exporter are not written yet. Nothing
here talks to Tripsy today.

See [CHANGELOG.md](CHANGELOG.md) for what has actually shipped.

## Installation

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/scanner/tripsy-exim.git
cd tripsy-exim
make setup
```

## Credentials

Tripsy's `POST /auth` trades a username and password for an API token.
`tripsy-exim` resolves credentials at run time, uses them, and never writes
them down -- no token file is created, and the token lives in memory for the
duration of the run.

Resolution order, first match wins:

1. A command line flag
2. An environment variable
3. `.env`
4. 1Password, via the `op` CLI

The 1Password form is preferred for scheduled runs, since it keeps
plaintext credentials out of the environment entirely:

```sh
export TRIPSY_ONEPASSWORD_URL="op://Personal/Tripsy"
```

That is an *item* URL. Its `username` and `password` fields are read with
`op read` when a command needs to authenticate.

## Repository layout

```text
tripsy_exim/
  api/       transport, auth, pagination, retries, error mapping, routes
  models/    canonical trip models and the local archive schema
  sources/   parsers turning an external format into canonical trips
  store/     the local archive on disk, plus the sync manifest
  sync/      importer and exporter -- all trip-level policy
  cli.py     command line entry point and credential resolution
```

The canonical models are the hinge. Parsers target them, the importer
writes them to Tripsy, the exporter rebuilds them from Tripsy, and the
archive stores them. Tripsy-specific concerns stay inside `api/`, so
supporting another service later is a new adapter rather than a rewrite.

Two design rules earn their place in the layout:

- **Imports are idempotent.** Tripsy treats `internal_identifier` as an
  idempotency key, so parsers mint a deterministic identifier from the
  source record. Re-running an import is a no-op, not a pile of duplicates.
- **Exports are incremental.** `updatedSince` reports trips changed
  directly *or* through their nested objects, so an unchanged trip is never
  fetched twice. Routine runs stay cheap; `--force` exists for repair.

## Development

```sh
make test       # test suite, skipping anything that needs the live API
make lint       # ruff, ruff-format, mypy via pre-commit
make coverage   # test suite with a coverage report
make help       # every target
```

Tests run against an in-memory fake of the Tripsy API, so the whole suite
works without credentials and without touching the real service.

### No personal data in this repository

Every example and test fixture is synthetically generated. No real trip,
itinerary, address, confirmation code, or name belongs in the repository or
the test suite, and nothing in a fixture should be traceable to a real
trip. Use a real `.ics` locally to check parsing if you need to, but do not
commit it.

## Reference

- [Tripsy public API documentation](https://docs.api.tripsy.app/)
- [Documentation source](https://github.com/tripsyapp/api)

## License

BSD 3-Clause. See [LICENSE](LICENSE).
