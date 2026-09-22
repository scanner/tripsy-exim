# export(1) -- take a dated copy of what Tripsy holds

## SYNOPSIS

```text
tripsy-exim export --all [--from DATE] [--to DATE]
tripsy-exim export [--trip ID|NAME]... [--glob PATTERN]
                   [--from DATE] [--to DATE]
                   [--archive-root DIRECTORY] [--verbose]
```

## RUNNING

Examples below are written as `uv run tripsy-exim`, which is how the
command runs from a fresh clone. See [the docs README](README.md) for
when you can drop the `uv run`.

## DESCRIPTION

`export` is the backup. It reads the account and writes a dated
directory holding one directory per trip, each with a single `trip.json`
and whatever files are attached to it in the app.

It is not the import run backwards. Nothing is compared against an
earlier run, nothing is merged into one, and nothing is pruned: each run
is a point in time that reads on its own. Two runs put side by side are
two backups, not a history.

The layout is documented in [archive(7)](archive.md#exports), down to how
a trip directory is named and why a document's download URL is not kept.

Nothing is written to a staging archive and nothing is sent to Tripsy.
`export` only reads.

### Saying what to take

A run has to be told what it covers. `--all` is every trip; `--trip` and
`--glob` name them; `--from` and `--to` narrow whatever was named.

Name selectors **union**. Naming two trips takes both, and a glob that
matches nothing still lets a named trip through.

The date range then **narrows** that union, matching on overlap: a trip
is in a range if any part of it was. A fortnight abroad over New Year is
in December and in January both.

```sh
uv run tripsy-exim export --glob 'Japan*' --from 2020-01-01
```

reads as "the Japan trips, from 2020 onwards" rather than as two
separate requests.

A trip whose `has_dates` is false is outside every range. The flag is
authoritative, so its date fields are not an answer, and a range asks a
question it has no answer to. `--all` is how to take those.

**Asking for nothing is refused.** A backup command whose bare form
silently meant "everything" would eventually be run by somebody who meant
something narrower, and a full pass of requests is what it costs to find
out.

## OPTIONS

`--all`
: Every trip the account holds. Cannot be combined with `--trip` or
  `--glob`, which narrow by name; it can be combined with a date range.

`--trip ID|NAME`
: One trip, by its Tripsy id or by part of its name. Repeatable. A value
  of digits is read as an id, anything else as part of a name, matched
  without regard to case.

`--glob PATTERN`
: Trips whose name matches a shell-style pattern -- `*`, `?`, `[abc]`.
  Matched without regard to case. Not a regular expression.

`--from DATE`, `--to DATE`
: Trips that were under way in this window, as `YYYY-MM-DD`. Either may
  be given alone.

`--archive-root DIRECTORY`
: Where the staging archives and the exports live. Defaults to
  `$TRIPSY_EXIM_ARCHIVE`, then `~/.local/share/tripsy-exim`.

`--username TEXT`, `--password TEXT`
: Credentials for this run. See the README on credentials for the
  resolution order and the secret stores.

`--verbose`
: Say how many trips were selected, then name each one as it is
  written, with its count of objects and documents. The summary then
  says how the run was paced: requests sent, how many the service
  throttled, how many failed, and the time spent waiting between them.
  Quiet by default, which is what a scheduled run wants.

## EXAMPLES

Everything, which is the ordinary backup:

```sh
uv run tripsy-exim export --all
```

One trip, by name:

```sh
uv run tripsy-exim export --trip 'Kyoto'
```

A year of them:

```sh
uv run tripsy-exim export --all --from 2011-01-01 --to 2011-12-31
```

From cron, monthly, quiet unless something happens:

```crontab
17 4 1 * *  cd ~/src/tripsy-exim && uv run tripsy-exim export --all
```

## DIAGNOSTICS

| Exit | Meaning |
|---|---|
| 0 | The export was written. |
| 1 | It failed, or the selection was refused before anything ran. |
| 2 | Another run holds the lock. Nothing was done. |

**2 is not a failure.** A nightly job starting while the previous one is
still going has nothing to do, and a scheduler that treats it as an error
wakes somebody for it. The separate code is what lets a scheduler tell
that apart from a backup that actually broke.

## NOTES

A run holds a lock on the exports directory for its whole length, so a
scheduled export and one started by hand cannot write at once. The lock
is held on an open file descriptor, so it is released however the run
ends, crash included -- a lock file left behind cannot wedge the runs
after it.

A run builds into a hidden directory beside the finished exports and
moves it into place at the end. A dated directory therefore never names a
half-written run. An interrupted one leaves only the hidden directory,
and the next run removes it before it starts.

A stamp names one run, so two runs started within the same second collide
and the second is refused. In practice the lock makes that unreachable
for overlapping runs, and a backup's cadence is weekly or monthly rather
than per second.

**An export costs a full pass of requests**: every trip and every child,
every time, through a v2 API paginated at 100. There is no watermark and
nothing incremental, by design. The answer is cadence -- weekly or
monthly is comfortable, hourly is not -- which is why this command runs
on the most patient of the three pacing profiles.

Nothing prunes exports. They accumulate and are managed by hand.

## SEE ALSO

[archive(7)](archive.md), [upload(1)](upload.md),
[verify(1)](verify.md), [list(1)](list.md)
