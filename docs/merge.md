# merge(1) -- upload one staged trip as part of another

## SYNOPSIS

```text
tripsy-exim merge [--archive NAME] ABSORBED TARGET
tripsy-exim merge [--archive NAME] --undo ABSORBED
```

## RUNNING

Examples below are written as `uv run tripsy-exim`, which is how the
command runs from a fresh clone. See [the docs README](README.md) for
when you can drop the `uv run`.

## DESCRIPTION

Declares that one staged trip should be uploaded into another rather than
created as a trip of its own. Nothing is sent to Tripsy, and no
credentials are needed.

One journey can reach the archive as two trips. A TripIt export records a
trip per traveller, so a holiday taken together arrives twice -- each
copy holding that traveller's own flights and their own room, under
different trip uuids. Uploading both makes two rival trips out of one
journey, and no amount of editing in the app merges them afterwards.

Nothing moves on disk. Both trips stay exactly as the parser produced
them, which is what keeps staging lossless and the declaration
reversible. The declaration is recorded in the manifest and read at
upload time and nowhere else.

At upload, `ABSORBED` is never created. Its objects are written into
`TARGET`, taking their place in that trip's single `sort_order` sequence.
Its own trip key stays in the archive, marked `->` by
[list(1)](list.md).

Trips are named by **key**, not by name -- the trips this is for share a
name, which is usually how they were found in the first place. Get the
keys from `uv run tripsy-exim list`.

Declare the merge before uploading either trip. Once a trip has been
created in Tripsy its identifier is spent, and merging it afterwards
would leave the rival trip standing.

## OPTIONS

`--undo`
: Release `ABSORBED` so it uploads as its own trip again. Takes no
  `TARGET`.

`--archive NAME`
: Staging archive the trips are read from. Named under `<root>/staged/`.
  Defaults to `staged`.

`--archive-root DIRECTORY`
: Where the staging archives and the exports live. Defaults to
  `$TRIPSY_EXIM_ARCHIVE`, then `~/.local/share/tripsy-exim`.

## DIAGNOSTICS

The declaration is refused when:

- the two keys are the same trip
- either key is not staged
- `TARGET` is itself absorbed into something else -- a chain nobody
  intended. Merge into the trip at the end of it instead.

## EXAMPLES

Find the duplicate pair, then declare one into the other:

```sh
uv run tripsy-exim list | grep 'Lakeside'
uv run tripsy-exim merge txim-tripit-json-g01-3ec9308... txim-tripit-json-g01-2e2e0dc...
uv run tripsy-exim upload --dry-run --trip txim-tripit-json-g01-2e2e0dc...
```

## FILES

Records the declaration under `merged_trips` in
`<archive>/manifest.json`.

## SEE ALSO

[list(1)](list.md), [upload(1)](upload.md), [archive(7)](archive.md)
