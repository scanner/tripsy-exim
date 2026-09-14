# stage(1) -- parse .ics files into the archive

## SYNOPSIS

```text
tripsy-exim stage [--archive DIRECTORY] [--namespace TEXT] [--scratch]
                  SOURCES...
```

## DESCRIPTION

Reads one or more TripIt `.ics` calendars and writes canonical trips into
the archive. Nothing is sent to Tripsy, and no credentials are needed.

Each calendar becomes a trip directory: the trip, its objects sorted into
`activities/`, `hostings/` and `transportations/`, and a `report.json`
recording what the parser made of the source. The report is the thing to
read before uploading -- it names the events that fell through to an
activity because nothing matched, the ones skipped outright, and the
times whose zone had to be guessed. See REPORT in
[archive(7)](archive.md).

`SOURCES` are `.ics` files; pass as many as you like. Directories are
not expanded -- let the shell do it.

Use this for the trips TripIt will still hand you a calendar for. For a
whole account at once, see [stage-export(1)](stage-export.md) -- and
stage the export **first**, since it is authoritative for a trip's
identity. A calendar naming a trip already staged from an export is
refused rather than staged a second time.

## OPTIONS

`--archive DIRECTORY`
: Where the canonical objects are written. Defaults to
  `$TRIPSY_EXIM_ARCHIVE`, then `~/.local/share/tripsy-exim/archive`.

`--namespace TEXT`
: Mint identifiers into this namespace instead of the one the source
  implies. Implies a shaping run -- a namespace you chose is not the one
  a real import uses. The namespace already carries its generation
  suffix, so pass the bare source name (`tripit-json`), never
  `tripit-json-g01`.

`--scratch`
: Mint into a fresh throwaway namespace, so the run can be uploaded,
  deleted, and redone without spending the real identifiers. See
  IDENTIFIERS in [archive(7)](archive.md).

## FILES

Writes `<archive>/trips/<trip key>/` and updates
`<archive>/manifest.json`. See [archive(7)](archive.md) for the layout.

## EXAMPLES

Stage one calendar, then read what the parser could not classify:

```sh
tripsy-exim stage ~/Downloads/tripit/Lakeside-2012.ics
jq '.unclassified[] | {summary, reason}' \
   "$TRIPSY_EXIM_ARCHIVE"/trips/txim-ics-g01-*/report.json
```

Stage a shelf of calendars into a throwaway namespace first, to see what
an upload would look like without spending identifiers:

```sh
tripsy-exim stage --scratch ~/Downloads/tripit/*.ics
tripsy-exim upload --dry-run --verbose
```

## SEE ALSO

[stage-export(1)](stage-export.md), [list(1)](list.md),
[upload(1)](upload.md), [archive(7)](archive.md)
