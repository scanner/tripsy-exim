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

The import path works end to end: a TripIt GDPR export or a `.ics` is
parsed into a local archive, corrected there, uploaded to Tripsy, read
back and checked, and anything Tripsy left unplaced on the map is
geocoded afterwards. The export side -- pulling a whole Tripsy account
back down into the archive -- is not written yet.

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

1. `--username` / `--password` on the command
2. `TRIPSY_USERNAME` / `TRIPSY_PASSWORD` in the environment, or in `.env`
3. A secret store named by `TRIPSY_SECRET_URL`

The store is last because it is the one form that keeps a plaintext
password out of the environment entirely, so anything more explicit is a
deliberate override of it. It is the form to prefer for scheduled runs:

```sh
export TRIPSY_SECRET_URL="op://Personal/Tripsy"
```

That is a 1Password *item* URL; its `username` and `password` fields are
read with the `op` CLI when a command needs to authenticate. Set
`TRIPSY_OP_BIN` if `op` is not on the `PATH`. The `hcvault://` scheme is
reserved for HashiCorp Vault and is not implemented yet.

Commands that only read or write the archive -- `stage`, `stage-export`,
`list`, `merge`, and `upload` without `--write` -- need no credentials at
all.

## Workflow

```sh
tripsy-exim stage-export ~/Downloads/tripit-export/export.json
tripsy-exim list
tripsy-exim upload --limit 1 --verbose     # plan only; the default
tripsy-exim upload --limit 1 --write
tripsy-exim verify --limit 1
tripsy-exim fix-locations --trip 'Lakeside' --write
```

Staging never touches the network, and `upload` plans and prints without
`--write`. That order is deliberate: Tripsy never releases an
`internal_identifier`, so an object created by mistake cannot be undone
by deleting it. Read the plan first.

Everything lands in one directory -- the archive -- named by `--archive`,
or `$TRIPSY_EXIM_ARCHIVE`, or `~/.local/share/tripsy-exim/archive`. What
the parser inferred is written there; corrections are kept beside it and
laid over on the way out, so re-staging never clobbers a decision made by
hand. See [archive(7)](docs/archive.md).

## Documentation

One page per command, plus one on the archive itself:

| Page | |
|---|---|
| [stage(1)](docs/stage.md) | Parse `.ics` files into the archive |
| [stage-export(1)](docs/stage-export.md) | Parse a TripIt GDPR export into the archive |
| [list(1)](docs/list.md) | List the staged trips and what has been uploaded |
| [merge(1)](docs/merge.md) | Upload one staged trip as part of another |
| [upload(1)](docs/upload.md) | Send staged trips to Tripsy |
| [verify(1)](docs/verify.md) | Read them back and compare against the plan |
| [fix-locations(1)](docs/fix-locations.md) | Geocode what Tripsy left without a position |
| [archive(7)](docs/archive.md) | The archive on disk, identifiers, overrides, the manifest |

## Repository layout

```text
tripsy_exim/
  api/        transport, auth, pagination, retries, error mapping, routes
  models/     canonical trip models, identifiers, the archive schema
  sources/    parsers turning an external format into canonical trips
  store/      the local archive on disk, plus the sync manifest
  sync/       importer, overrides, verification -- all trip-level policy
  geocode.py  positions for what Tripsy will not place itself
  secrets.py  resolving credentials from a secret store
  cli.py      command line entry point
docs/         one page per command, plus the archive
```

The canonical models are the hinge. Parsers target them, the importer
writes them to Tripsy, the exporter rebuilds them from Tripsy, and the
archive stores them. Tripsy-specific concerns stay inside `api/`, so
supporting another service later is a new adapter rather than a rewrite.

Three design rules earn their place in the layout:

- **Imports are idempotent.** Tripsy treats `internal_identifier` as an
  idempotency key, so parsers mint a deterministic identifier from the
  source record. Re-running an import is a no-op, not a pile of duplicates.

  Two limits are worth knowing before a large import. Suppression is
  scoped to one collection of one trip, so an object that changes *type*
  between runs is created afresh alongside the original rather than moved.
  And deleting a trip does not release its identifier -- a deleted trip
  cannot be brought back by re-running the import that created it.
- **Exports are incremental.** `updatedSince` reports trips changed
  directly *or* through their nested objects, so an unchanged trip is never
  fetched twice. Routine runs stay cheap; `--force` exists for repair.
- **Every call is paced.** Tripsy publishes no rate limit and sends no
  rate-limit headers, so there is nothing to read and no way to find a
  limit except by hitting it. The client sets its own pace from the one
  signal the API does give -- how long it takes to answer -- and waits
  roughly as long as the last response took before sending the next.
  A fast API is still paced; a slowing one is backed away from, up to a
  ceiling. Should a `Retry-After` ever arrive it takes precedence, raising
  the floor for the rest of the run and decaying back as calls succeed.

  Pacing lives in an `httpx` transport rather than in each route, so it
  cannot be gone around, and the pace is shared across a whole run rather
  than per-request. Three profiles match how this gets used:

  | Profile | For | Gap per second of latency | Floor | Ceiling | Timeout |
  |---|---|---|---|---|---|
  | `import` | a bulk load of thousands of writes | 1.0x | 0.1s | 5s | 30s |
  | `backup` | a scheduled or manual export | 2.0x | 0.5s | 10s | 60s |
  | `interactive` | a few calls with someone waiting | 0.5x | 0.05s | 2s | 15s |

  A request that never answers feeds back into all three of pacing,
  retrying, and reporting, and it is worth being explicit about how:

  - **As a latency sample.** A timeout reports the full timeout as its
    elapsed time, so the pace rises to the ceiling on its own. Without
    this a timeout storm would be the one case producing no slowdown at
    all. A refused connection returns almost instantly instead, so it is
    also treated as a reason to back off directly -- otherwise it would
    look like the fastest response of the run and *raise* the pace.
  - **As a retry.** A timeout is retried on the same terms as any other
    failure, which matters because a timed-out write may well have
    landed. That is safe for the same reason any create is retryable: the
    minted `internal_identifier` makes the second attempt a no-op. A
    create without one is not repeated after a timeout, any more than
    after a `502`.
  - **In the counters.** Timeouts count as failures and `429`/`503`
    count as throttles, kept apart so a run can be reviewed afterwards:
    "the service asked us to slow down" and "the service stopped
    answering" call for different responses.

  The timeout is also what bounds the worst latency sample the pacer can
  ever see, so one hung connection cannot define the pace for a whole
  run.

## Development

```sh
make test       # test suite, skipping anything that needs the live API
make lint       # ruff, ruff-format, mypy via pre-commit
make coverage   # test suite with a coverage report
make help       # every target
```

Tests run against an in-memory fake of the Tripsy API, so the whole suite
works without credentials and without touching the real service.

A `probe_scripts/` directory may be present locally. Those are one-off
scripts run by hand against the live API to answer a question about how
Tripsy actually behaves -- what it geocodes, what it accepts. They are
not part of the package, are not tested, and are deliberately not
committed.

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
