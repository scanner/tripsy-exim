# archive(7) -- the local trip archive

## NAME

archive -- what `tripsy-exim` keeps on disk, and how corrections are laid
over it

## DESCRIPTION

Every command reads or writes one staging archive. It holds the
canonical form of every trip staged from a source, the corrections made
to them, and a record of what has been uploaded and what Tripsy called
it.

Staging archives are named, and several can live side by side -- the
TripIt import in one, a scratch archive in another. A command works on
the one `--archive` names, or on `staged` when it names none.

The archive is not a cache. It is the provider-neutral copy of the data
-- the reason for the project's second half -- so it is kept in durable
storage, it is meant to be backed up, and it is safe to read with
ordinary tools.

Staging is lossless. A parser writes what it inferred and nothing else,
so re-staging a source always produces the same files and never clobbers
a decision made by hand. Decisions live in `overrides/`, keyed
separately, and are applied on the way out to Tripsy.

## LOCATION

One root holds every archive this tool keeps, of two kinds:

```text
<root>/
  staged/<name>/                 staging archives; the default is 'staged'
  exports/<stamp>/               one directory per export run
```

An export is a different shape -- a document per trip rather than a file
per object -- and is described under [EXPORTS](#exports) below. Until
then this page is about a staging archive.

For the root, first match wins:

1. `--archive-root DIRECTORY` on any command
2. `$TRIPSY_EXIM_ARCHIVE`
3. `$XDG_DATA_HOME/tripsy-exim`, or `~/.local/share/tripsy-exim`

### Moving an archive made before the split

An archive made when the root *was* the archive has to be moved down a
level. It cannot be moved into itself, so it goes aside first:

```sh
ROOT="$TRIPSY_EXIM_ARCHIVE"          # or ~/.local/share/tripsy-exim
mv "$ROOT" "$ROOT.moving"
mkdir -p "$ROOT/staged"
mv "$ROOT.moving" "$ROOT/staged/staged"
```

Until that is done every command reports `no archive directory at
<root>/staged/staged`, naming a path that has never existed. Nothing is
lost by waiting: the move is the whole migration, and an archive's own
contents are unchanged by it.

## NAMING AN ARCHIVE

`--archive NAME` picks which staging archive under the root. A name is a
single directory component: letters, digits, `.`, `-` and `_`. Runs of
whitespace become one `_`, so `--archive 'old import'` and
`--archive old_import` are the same archive; anything else is refused
rather than quietly repaired, since a silently renamed archive is how two
of them end up on disk.

Accented and non-Latin names are refused for a narrower reason. macOS
normalises filenames, so a name typed with a combining accent and the
same name typed precomposed do not find each other again -- and an
archive name is retyped on every later command. Trip directories inside
an export carry no such restriction: nothing retypes those.

## STAGING ARCHIVE LAYOUT

```text
<root>/staged/<name>/
  manifest.json                  what has been uploaded, and its Tripsy ids
  trips/
    <trip key>/
      trip.json                  the trip itself
      report.json                what the parser made of the source
      activities/<key>.json
      hostings/<key>.json
      transportations/<key>.json
      expenses/<key>.json
      collaborators/<key>.json
  overrides/
    <source uuid>.json           corrections, one file per trip
  quarantine/
    <key>.json                   payloads the models could not parse
```

`expenses/` and `collaborators/` are written by a read back from Tripsy,
not by staging: no source this project parses carries either, and the API
offers no way to create a collaborator at all. A staged trip has neither
directory until something has been read back into it.

A *trip key* is the trip's `internal_identifier`, and it names both the
directory and the trip. Object files are named by their own identifier.
Objects created in the Tripsy app rather than staged here carry a
`tripsy-<id>` key instead, since they were never minted from a source.

Every file is JSON, indented, with keys sorted, so a diff between two
runs is readable.

## EXPORTS

An export is what Tripsy held at one instant. It accumulates nothing: each
run writes a fresh directory that reads on its own, and two runs are never
compared.

```text
<root>/exports/<stamp>/
  manifest.json                  when, and what was asked for
  <start>--<end>--<id>/
    trip.json                    the trip and all its children
    documents/<id>-<title>       whatever was attached, as it was
  quarantine/<key>.json          payloads the models would not take
```

The stamp is the UTC instant the run started, written without colons
because not every filesystem takes one in a directory name.

A trip directory is named to sort by travel date, since that is the order
somebody reading a backup wants. The Tripsy id makes it unique: two trips
can share a name and dates, and a pair recording one journey twice is
exactly what [merge(1)](merge.md) exists for. A trip whose `has_dates` is
false is named `undated--<id>` even when the date fields are populated --
the flag is authoritative.

Inside, a trip is **one document** rather than a file per object, because
an export is read whole, by a person or by a program loading it, and never
by this tool looking one object up. A staging archive is the other way
round, which is why it keeps the other shape.

```json
{
  "schema_version": 1,
  "trip": { "id": 4071, "name": "Kyoto, May 2011" },
  "transportations": [ ... ],
  "hostings": [ ... ],
  "documents": [ ... ]
}
```

### Attachments

Files attached in the app are downloaded into `documents/` beside the
trip, named `<id>-<title>` so two files of one name cannot collide and a
person can still tell them apart. The title keeps its extension, which is
what makes the saved file open in the right thing.

A document's entry in `trip.json` is the payload as the API sent it, with
one field removed: `temp_read_url` is pre-signed and expires, so recording
it would archive a link that is dead by the time anybody follows it. What
is recorded instead is `file`, naming the copy on disk.

The `activities`, `hostings` and `transportations` arrays on a document
say which object it belongs to. A boarding pass belongs to a flight, not
to a fortnight, and those arrays are the only thing that says so.

### Whole or not at all

A run builds into `.<stamp>.partial` beside the finished exports and moves
it into place at the end. A stamp therefore never names a half-written
run: an interrupted one leaves only the dotted directory, and the next
run removes it before it starts.

Two runs of the same second would want the same name. The second is
refused rather than merged -- merging would make one directory two
instants, which is the one thing an export must not be.

### Scope

`manifest.json` records what the run was asked for, because a partial
export has to say it is one. Without it a directory cannot say whether it
means "everything Tripsy held" or "these trips", and the two are read
differently by anybody restoring from it.

Nothing prunes exports. They accumulate and are managed by hand.

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

See IDENTITY in [models(7)](models.md) for what `internal_identifier` is
and which objects carry one. This section is about the values
`tripsy-exim` mints into it.

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

Deleting the manifest does not lose trip data, but it does lose the
knowledge of what was already uploaded -- which is harmless, since a
re-run is a no-op, but it costs a full pass of requests.

## SEE ALSO

[models(7)](models.md), [stage(1)](stage.md),
[stage-export(1)](stage-export.md), [list(1)](list.md),
[merge(1)](merge.md), [upload(1)](upload.md), [verify(1)](verify.md),
[fix-locations(1)](fix-locations.md),
[export(1)](export.md)
