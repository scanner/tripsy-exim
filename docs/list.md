# list(1) -- list the trips staged in the archive

## SYNOPSIS

```text
tripsy-exim list [--archive DIRECTORY] [--pending | --all]
```

## DESCRIPTION

Prints the staged trips oldest first, one per line, with the trip key
that every other command uses to name a trip. Nothing is sent to Tripsy,
and no credentials are needed.

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

`--archive DIRECTORY`
: Where the staged trips are read from. Defaults to
  `$TRIPSY_EXIM_ARCHIVE`, then `~/.local/share/tripsy-exim/archive`.

`--pending`
: List only trips no run has finished uploading.

`--all`
: List every staged trip. The default.

## EXAMPLES

What the next upload would take:

```sh
tripsy-exim list --pending | head -10
```

## SEE ALSO

[upload(1)](upload.md), [merge(1)](merge.md), [archive(7)](archive.md)
