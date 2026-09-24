# list(1) -- list staged trips, or the trips in Tripsy

## SYNOPSIS

```text
tripsy-exim list [--archive NAME] [--all | --pending | --uploaded]
                 [--username TEXT] [--password TEXT]
```

## RUNNING

Examples below are written as `uv run tripsy-exim`, which is how the
command runs from a fresh clone. See [the docs README](README.md) for
when you can drop the `uv run`.

## DESCRIPTION

Prints trips oldest first, one per line. `--all` and `--pending` read
the staged archive; `--uploaded` asks Tripsy.

With `--all` or `--pending`, each staged trip is shown with the trip key
every other command uses to name it. Nothing is sent to Tripsy, and no
credentials are needed.

```text
  up  2012-10-03  A trip an earlier run finished uploading      txim-...
  ->  2012-10-03  A trip declared as part of another            txim-...
      2013-04-11  A trip nothing has uploaded yet               txim-...

78 trips, 21 already uploaded
```

The mark in the first column:

| Mark | Meaning |
|---|---|
| `up` | A run finished uploading this trip. `upload` steps over it. |
| `->` | Declared as part of another trip, so it uploads into that one rather than as itself. See [merge(1)](merge.md). |
| (blank) | Nothing has uploaded it. |

Oldest first is the same order `upload` works in, so `list --pending` is
a preview of what `upload --limit N` will take next.

## OPTIONS

`--archive NAME`
: Staging archive the trips are read from. Named under `<root>/staged/`.
  Defaults to `staged`.

`--archive-root DIRECTORY`
: Where the staging archives and the exports live. Defaults to
  `$TRIPSY_EXIM_ARCHIVE`, then `~/.local/share/tripsy-exim`.

`--all`
: List every staged trip. The default.

`--pending`
: List only staged trips no run has finished uploading.

`--uploaded`
: List the trips in your Tripsy account instead, read from Tripsy. Needs
  credentials -- see [auth(1)](auth.md) -- and no archive. When the
  staged archive exists, a trip that came from it is shown with its key,
  and the summary counts them:

  ```text
        2024-05-10  Osaka, Japan, May 2024                  txim-...
        2030-01-01  A trip made in the app

  2 trips in Tripsy, 1 from this archive
  ```

`--username TEXT`, `--password TEXT`
: Credentials for `--uploaded`, if a login is needed.

## EXAMPLES

What the next upload would take:

```sh
uv run tripsy-exim list --pending | head -10
```

Everything in your Tripsy account, with no archive at all:

```sh
uv run tripsy-exim list --uploaded
```

## SEE ALSO

[upload(1)](upload.md), [merge(1)](merge.md), [auth(1)](auth.md),
[archive(7)](archive.md)
