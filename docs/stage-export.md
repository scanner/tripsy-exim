# stage-export(1) -- parse a TripIt GDPR export into the archive

## SYNOPSIS

```text
tripsy-exim stage-export [--archive DIRECTORY] [--namespace TEXT]
                         [--scratch] EXPORT
```

## DESCRIPTION

Reads a TripIt GDPR export -- the JSON file TripIt sends when you ask for
your data -- and stages every trip in it in one pass. Nothing is sent to
Tripsy, and no credentials are needed.

One export carries a whole account, so this is the way in for the bulk of
a history. Each trip gets its own directory with a `report.json` beside
it naming the records that fell through to an activity, the ones skipped,
and the times whose zone had to be guessed. See REPORT in
[archive(7)](archive.md).

**Stage the export before any calendars.** The export is authoritative
for a trip's identity: it carries the trip's own uuid, dates and name,
where a `.ics` carries only what a calendar can express. A `.ics` naming
a trip already staged from an export is refused rather than staged a
second time.

The reader takes the export's own account of times and text: it reads a
record's clock in the timezone the record names, falls back to the
paired instant's zone when a record names none, and strips the markup
TripIt leaves in its notes.

## OPTIONS

`--archive DIRECTORY`
: Where the canonical objects are written. Defaults to
  `$TRIPSY_EXIM_ARCHIVE`, then `~/.local/share/tripsy-exim/archive`.

`--namespace TEXT`
: Mint identifiers into this namespace instead of the one the export
  implies. Implies a shaping run. The namespace already carries its
  generation suffix, so pass the bare source name (`tripit-json`), never
  `tripit-json-g01` -- passing the suffixed form mints a second suffix
  and rewrites the archive under identifiers nothing else knows.

`--scratch`
: Mint into a fresh throwaway namespace, so the run can be uploaded,
  deleted, and redone without spending the real identifiers.

## FILES

Writes `<archive>/trips/<trip key>/` for every trip in the export and
updates `<archive>/manifest.json`.

## EXAMPLES

Stage a whole account:

```sh
tripsy-exim stage-export ~/Downloads/tripit-export/export.json
tripsy-exim list
```

## NOTES

Re-running over the same export is safe and idempotent: identifiers are
derived from the source records, so the same files are written again.
Corrections are not touched, since they live in `overrides/` rather than
in the staged files.

## SEE ALSO

[stage(1)](stage.md), [list(1)](list.md), [upload(1)](upload.md),
[archive(7)](archive.md)
