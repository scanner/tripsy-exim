# verify(1) -- read uploaded trips back and compare them to the plan

## SYNOPSIS

```text
tripsy-exim verify [--archive NAME] [--trip TEXT]... [--limit N]
                   [--verbose] [--username TEXT] [--password TEXT]
```

## RUNNING

Examples below are written as `uv run tripsy-exim`, which is how the
command runs from a fresh clone. See [the docs README](README.md) for
when you can drop the `uv run`.

## DESCRIPTION

Reads trips back from Tripsy and reports what is there against what was
meant to be there. **Nothing is written.**

This is the check after an [upload(1)](upload.md) `--write`. It answers
three questions:

- **Missing** -- an object the plan holds that the account does not. A
  create that failed, or one that never ran.
- **Extra** -- an object the account holds that the plan does not.
  Something added in the app, or an object created under an identifier
  from an earlier attempt. Listed with `--verbose`.
- **Differing** -- a field that arrived as something other than what was
  planned, `sort_order` most often. Since `sort_order` is assigned at
  create and never revisited, a mismatch here is permanent until someone
  edits it in the app.

It also counts objects carrying an address and no position. Nothing on
the server resolves those: the app geocodes an activity's address when it
renders it, and a transportation endpoint is never geocoded at all. So an
unplaced activity may place itself once the trip is opened, and an
unplaced leg will not. [fix-locations(1)](fix-locations.md) places the
rest.

```text
A trip that came out right  [ok]  47 of 47 planned
A trip that did not  [DIFFERS]  46 of 47 planned
    missing   txim-tripit-json-g01-...
    sort_order  A hotel on the second night     planned 12, found 13
    3 addresses not yet placed
```

## OPTIONS

`--trip TEXT`
: Check only this trip, named by part of its name or by its key.
  Repeatable. The default is every trip an earlier run finished.

`--limit N`
: Check at most this many trips, oldest first.

`--verbose`
: List every object the account holds that the plan does not, and every
  unplaced address by name.

`--username TEXT`, `--password TEXT`
: Credentials for this run. See CREDENTIALS in the
  [README](../README.md).

`--archive NAME`
: Staging archive the trips are read from. Named under `<root>/staged/`.
  Defaults to `staged`.

`--archive-root DIRECTORY`
: Where the staging archives and the exports live. Defaults to
  `$TRIPSY_EXIM_ARCHIVE`, then `~/.local/share/tripsy-exim`.

## EXAMPLES

Check the trip just uploaded:

```sh
uv run tripsy-exim upload --limit 1 --write
uv run tripsy-exim verify --limit 1 --verbose
```

Check everything uploaded so far:

```sh
uv run tripsy-exim verify
```

## NOTES

An *extra* object is not necessarily a fault -- anything added in the app
after the upload shows up as one. Read the list before treating it as a
problem, and remember that the archive is not updated by anything done in
the app.

## SEE ALSO

[upload(1)](upload.md), [fix-locations(1)](fix-locations.md),
[archive(7)](archive.md)
