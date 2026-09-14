# archive(7) -- the local trip archive

## NAME

archive -- what `tripsy-exim` keeps on disk, and how corrections are laid
over it

## DESCRIPTION

Every command reads or writes one directory: the archive. It holds the
canonical form of every trip staged from a source, the corrections made
to them, and a record of what has been uploaded and what Tripsy called
it.

The archive is not a cache. It is the provider-neutral copy of the data
-- the reason for the project's second half -- so it is kept in durable
storage, it is meant to be backed up, and it is safe to read with
ordinary tools.

Staging is lossless. A parser writes what it inferred and nothing else,
so re-staging a source always produces the same files and never clobbers
a decision made by hand. Decisions live in `overrides/`, keyed
separately, and are applied on the way out to Tripsy.

## LOCATION

First match wins:

1. `--archive DIRECTORY` on any command
2. `$TRIPSY_EXIM_ARCHIVE`
3. `$XDG_DATA_HOME/tripsy-exim/archive`, or
   `~/.local/share/tripsy-exim/archive`

## LAYOUT

```text
<archive>/
  manifest.json                  what has been uploaded, and its Tripsy ids
  trips/
    <trip key>/
      trip.json                  the trip itself
      report.json                what the parser made of the source
      activities/<key>.json
      hostings/<key>.json
      transportations/<key>.json
  overrides/
    <source uuid>.json           corrections, one file per trip
```

A *trip key* is the trip's `internal_identifier`, and it names both the
directory and the trip. Object files are named by their own identifier.
Objects created in the Tripsy app rather than staged here carry a
`tripsy-<id>` key instead, since they were never minted from a source.

Every file is JSON, indented, with keys sorted, so a diff between two
runs is readable.

## REPORT

`report.json` sits beside each staged trip and records what the parser
made of the source. It is not read by anything -- it exists to be read by
a person, before an upload spends identifiers.

| Key | Meaning |
|---|---|
| `trip_key`, `trip_uuid` | The trip this report is about, by minted identifier and by source uuid. |
| `counts` | How many objects landed in each collection. |
| `index` | Source uuid to the collection and identifier it became. This is the map from a correction's key to the object it corrects. |
| `unclassified` | Events that fell through to an activity because nothing matched, each with the reason. Most of a TripIt corpus lands here. |
| `skipped` | Source records that produced no object at all. |
| `guessed_timezones` | Times whose zone the source did not give, and where the parser got one from instead. |

To correct something, look its source uuid up in `index`, then write an
override under that uuid.

## IDENTIFIERS

Tripsy treats `internal_identifier` as an idempotency key: POSTing an
object whose identifier already exists returns an empty `200` rather than
creating a second one. That is what makes a re-run of an upload a no-op
instead of a pile of duplicates, and it is why identifiers are derived
rather than random.

An identifier reads left to right:

```text
txim-tripit-json-g01-52b4a7ef87e23b68
 |    |          |   |
 |    |          |   `-- digest of the source record
 |    |          `------ generation
 |    `----------------- namespace: the source it came from
 `---------------------- minted by tripsy-exim
```

The same source record always mints the same identifier -- across runs,
machines, and rebuilds of the archive. Rebuilding the archive from the
same export and uploading again therefore writes nothing new.

**Identifiers are never released.** A deleted object keeps its own, so
deleting a trip in the app and re-running the upload that created it
brings back nothing. The only way back in is a different identifier for
the same record, which is what the *generation* segment is for:
generation 2 is a second attempt at a record whose first identifier was
spent. Raising a generation is a deliberate act, not something a re-run
does.

`--scratch` mints into a throwaway namespace instead, so an upload can be
tried, deleted, and tried again without spending the real identifiers.
Nothing staged with `--scratch` should ever be treated as the real
archive.

## SORT ORDER

Tripsy orders a day's objects by `sort_order`, which is one dense
sequence across the whole trip rather than per collection or per day. It
is assigned when an object is created and never revisited: a later run
that finds the object already there leaves its position alone. So the
order a trip reads in is decided by the run that first uploaded it, and
changing it afterwards is an edit in the app, not a re-run.

## OVERRIDES

The parser guesses. Most of a TripIt corpus falls through to an activity
because nothing matched, and those guesses have to be correctable without
editing the staged files -- re-staging would clobber an edit, and *what
the parser inferred* has to stay separable from *what I decided*.

Corrections live in `overrides/<source uuid>.json`, one file per trip.
They are keyed by the source record's uuid, which is the one identity
that survives both a re-export and a change of identifier namespace, so a
correction made during a shaping run carries into the real one.

Three kinds:

| Kind | What it does |
|---|---|
| retype | Move an object to another collection -- an activity that is really a transportation. Fields are carried across the two shapes. |
| correct | Replace named fields on one object: an address, a time, a title. |
| add | Introduce an object no source record held -- the shuttle nobody booked. |

Overrides are applied on the way out, during `upload`, and nowhere else.
`stage` and `stage-export` ignore them entirely.

## MANIFEST

`manifest.json` is the archive's record of its relationship with Tripsy:

| Key | Meaning |
|---|---|
| `schema_version` | Layout version of the archive. |
| `trip_index` | Trip name and dates to the trip keys staged under them, used to find the trips a `merge` is about. |
| `identifier_cache` | `internal_identifier` to the numeric Tripsy id it was given, so an uploaded object can be read back without searching. |
| `uploaded_trips` | Trip key to when a run finished uploading it. This is what `list` marks and what `upload` steps over. |
| `merged_trips` | Absorbed trip key to the trip it uploads into. See [merge(1)](merge.md). |
| `last_export_at` | High-water mark for incremental export. |

Deleting the manifest does not lose trip data, but it does lose the
knowledge of what was already uploaded -- which is harmless, since a
re-run is a no-op, but it costs a full pass of requests.

## SEE ALSO

[stage(1)](stage.md), [stage-export(1)](stage-export.md),
[list(1)](list.md), [merge(1)](merge.md), [upload(1)](upload.md),
[verify(1)](verify.md), [fix-locations(1)](fix-locations.md)
