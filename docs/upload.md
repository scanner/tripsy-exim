# upload(1) -- upload staged trips to Tripsy

## SYNOPSIS

```text
tripsy-exim upload [--archive NAME] [--trip TEXT]... [--limit N]
                   [--write | --dry-run] [--force] [--verbose]
                   [--username TEXT] [--password TEXT]
```

## RUNNING

Examples below are written as `uv run tripsy-exim`, which is how the
command runs from a fresh clone. See [the docs README](README.md) for
when you can drop the `uv run`.

## DESCRIPTION

Plans what it would send and prints the plan. **Without `--write` that is
all it does**: nothing is sent, no credentials are needed, and the
numbers shown are the ones a real run would send.

The dry run is the default on purpose. An identifier Tripsy has seen is
never released, so a mistaken create cannot be taken back by deleting the
trip -- see IDENTIFIERS in [archive(7)](archive.md). Plan, read the plan,
then run it again with `--write`.

Trips go oldest first, so `--limit` works forward through an account
rather than picking an arbitrary handful. A trip an earlier run finished
is stepped over without a request and does not count against `--limit`,
so `--limit 1` run repeatedly walks the archive a trip at a time.

Re-running is a no-op rather than a source of duplicates: an object whose
identifier Tripsy already holds comes back as an empty `200`. A run that
failed part way is resumed by running it again.

Corrections are applied here. `stage` writes what the parser inferred;
`upload` lays the overrides for a trip over that on the way out. Trips
declared with [merge(1)](merge.md) are folded into their target and not
created themselves.

Coordinates go up with the create wherever the archive has them, which is
the only time they cost nothing. What is left unplaced afterwards is
[fix-locations(1)](fix-locations.md)'s work.

## OPTIONS

`--write`
: Actually send to Tripsy.

`--dry-run`
: Plan and print without writing. The default.

`--trip TEXT`
: Upload only this trip, named by part of its name or by its key.
  Repeatable. The default is every trip.

`--limit N`
: Upload at most this many trips, oldest first. Trips an earlier run
  finished do not count against it.

`--force`
: Include trips an earlier run already finished. Safe -- the creates are
  no-ops -- but it spends a full pass of requests.

`--verbose`
: List every object a trip would write, not just the totals.

`--username TEXT`, `--password TEXT`
: Credentials for this run. See CREDENTIALS in the
  [README](../README.md); only needed with `--write`.

`--archive NAME`
: Staging archive the trips are read from. Named under `<root>/staged/`.
  Defaults to `staged`.

`--archive-root DIRECTORY`
: Where the staging archives and the exports live. Defaults to
  `$TRIPSY_EXIM_ARCHIVE`, then `~/.local/share/tripsy-exim`.

## EXAMPLES

The incremental way through an account -- plan one trip, send it, look at
it in the app, then take the next:

```sh
uv run tripsy-exim upload --limit 1 --verbose
uv run tripsy-exim upload --limit 1 --write
uv run tripsy-exim verify --limit 1
```

Widen once the shape of what arrives is no longer a surprise:

```sh
uv run tripsy-exim upload --limit 10 --write
```

One named trip:

```sh
uv run tripsy-exim upload --trip 'Lakeside' --write
```

## DIAGNOSTICS

Exits non-zero when any object fails to write, reporting how many. The
trip is not marked as uploaded, so running again picks up where it
stopped.

## NOTES

Every request is paced. Tripsy publishes no rate limit and sends no
rate-limit headers, so the client sets its own pace from how long the
service takes to answer. `upload` uses the `import` profile, tuned for a
bulk load of thousands of writes. See the README for the profiles.

## SEE ALSO

[list(1)](list.md), [merge(1)](merge.md), [verify(1)](verify.md),
[fix-locations(1)](fix-locations.md), [archive(7)](archive.md)
