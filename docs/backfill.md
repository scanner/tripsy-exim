# backfill(1) -- author corrections against what the archive knows

## SYNOPSIS

```text
tripsy-exim backfill infer  [--archive DIRECTORY] [--disagree-km FLOAT]
                            [--write | --dry-run] [TRIP]...
tripsy-exim backfill report [--archive DIRECTORY]
                            [--population NAME]... [TRIP]...
tripsy-exim backfill export FILE [--archive DIRECTORY]
                            [--population NAME]... [TRIP]...
tripsy-exim backfill apply  FILE [--archive DIRECTORY]
                            [--write | --dry-run]
```

## RUNNING

Examples below are written as `uv run tripsy-exim`, which is how the
command runs from a fresh clone. See [the docs README](README.md) for
when you can drop the `uv run`.

## DESCRIPTION

The archive supports three kinds of correction and
[archive(7)](archive.md) documents all three, but writing one means
reading `report.json`, finding a source uuid in its index, and hand-
writing a file under `overrides/`. That is fine for one correction and
hopeless for a hundred.

`backfill` is the other end of that. It reads the parser's own doubts
back out, together with the objects Tripsy would have no way to place,
and says what is still open -- so corrections are written against a list
instead of against a directory of JSON files.

Run them in this order:

1. **`infer`** fills what the archive can work out from itself, with no
   human input. Run it first, so the list is shorter by the time you
   read it.
2. **`report`** says what is left. It writes nothing.
3. **`export`** turns what is left into an editable file.
4. **`apply`** reads that file back as corrections.

### The four populations

Three are the parser's, recorded beside the trip when it staged:

| population         | what it means                                   | what closes it   |
|--------------------|-------------------------------------------------|------------------|
| `unclassified`     | No rule matched, so it was filed as an activity | a **retype**     |
| `guessed_timezone` | The zone was guessed rather than read           | a **correction** |
| `skipped`          | No object was produced at all                   | an **addition**  |

The fourth the parser does not know it has:

| population    | what it means                     | what closes it   |
|---------------|-----------------------------------|------------------|
| `unplaceable` | Neither an address nor a position | a **correction** |

`guessed_timezone` only ever comes from a calendar. A zone is derived
from an event's coordinates, and an event carrying none inherits its
neighbour's -- which is a guess, and wrong for anything that crossed a
border. A TripIt JSON export either says outright what zone a record is
in or says nothing, so a trip staged with
[stage-export(1)](stage-export.md) never has one.

### Why "unplaceable" is one question and not two

Tripsy geocodes an address by itself, and draws the pin from a stored
position *in preference to* the address when one is there. So an object
carrying an address needs no coordinates from us, and an object carrying
coordinates needs no address. Only an object with **neither** is
actually unplaced.

Counting the two fields separately would report thousands of perfectly
placed objects as incomplete, and worse, invite someone to fill in
coordinates for an object the app was already placing exactly -- which
replaces a precise pin with a borrowed one.

The case is rarer than it sounds and real. A TripIt export keeps an
airport's code and its name in separate fields: the leg's label reads
`start_airport_code` while its address falls back to
`start_airport_name`. An endpoint can therefore say `NRT` and carry
nothing to place it by. Across the reference corpus this was 4 endpoints
out of 522.

### Not the same as fix-locations

[fix-locations(1)](fix-locations.md) is the opposite case, on the other
side of an upload. It works from Tripsy, on objects that **have** an
address and no position, and asks a geocoder. An unplaceable object has
no address to ask about, so nothing can look it up -- the answer comes
from elsewhere in the archive, or from a person.

### Corrections already written are taken into account

Gaps are counted against each trip *as that trip would upload*, with any
corrections already authored laid over. Corrections are never written
back to the staged files -- staging owns those, and re-staging rewrites
them -- so a report that read the directory would keep reporting a gap
long after it had been answered.

Answering something and running again therefore reports one fewer, not
the same list.

### The order the places are listed in

Places are listed commonest first, because that is the order that
finishes soonest. One airport or station recurs across a whole corpus,
so answering it once closes every gap that names it; the long tail of
places seen exactly once is what is left afterwards. Ties are broken
alphabetically, so the same archive prints the same work-list every run.

## SELF-HEALING

An export places nearly everything it carries -- 518 of 522 airport
endpoints across the reference corpus. The four it missed named codes
that *other segments had placed*. So for those, the answer was already
on disk and no outside service had to be asked for it.

`infer` does that lookup. For each endpoint the app cannot place, it
takes the code, finds every position the archive records for that code,
and fills in the one they agree on.

**Only exact keys.** An airport code is three letters and nothing else,
so two endpoints naming `NRT` are naming one airport. A station gives
its name instead -- `Shin-Osaka Station` -- and free text is not a key:
two spellings of one station are two keys, and one spelling can be two
stations. Endpoints named in free text are skipped entirely rather than
refused, because they were never candidates.

**The commonest position, not the first**, so one odd record cannot move
an airport.

**A code observed in places far apart is refused**, not averaged. The
modal rule protects against a stray record; it does nothing about a code
genuinely used for two places, which is the realistic hazard.

The `--disagree-km` default of 10 is measured against the two things it
has to tell apart:

| | distance |
|---|---|
| One airport's own published variants -- terminal, centroid, runway | ~1.4 km |
| SFO to OAK, the closest pair a reused code could confuse | 17.3 km |

Ten sits an order of magnitude above the first and well below the
second. The Bay Area is what calibrated it: SFO to OAK is 17 km, OAK to
SJC 47 km, SFO to SJC 49 km, so a looser threshold waves every pair
through.

A refusal costs nothing -- the endpoint stays open and shows up in
`backfill report`, which is where it already was. A wrong placement is
silent and goes through a one-way upload. So the guard errs tight, and
a refusal says the distance it saw.

> If your archive holds both a JSON export and `.ics` calendars, expect
> some refusals. A calendar's coordinates name a destination city rather
> than its terminal and can sit tens of kilometres from the export's --
> a real disagreement about one place, which is exactly what the guard
> is built to stop and hand to you.

## THE WORK-LIST

`export` writes one JSON row per open gap. Each row carries the source
uuid a correction is keyed by, the model field names it would set, and
the values the object holds right now:

```json
{
  "trip": "Kyoto, May 2011",
  "trip_key": "txim-tripit-json-g01-2440a45a...",
  "uuid": "b3de6dbb-a4fd-5545-9abb-8156d9970a56",
  "identifier": "txim-tripit-json-g01-117b488f...",
  "population": "unplaceable",
  "object": "NAR (departure)",
  "fields": {
    "departure_address": "",
    "departure_latitude": "",
    "departure_longitude": ""
  }
}
```

Real field names rather than logical ones, so the file says plainly what
it will set and applying a row is mechanical.

Three conventions, all of them about telling *not answered* apart from
*answered with nothing*:

| in the file | what it means |
|---|---|
| an empty string | a blank left deliberately blank; skipped |
| an unchanged value | already correct; skipped |
| a key in `fields` ending `_note` | commentary for a reader; never written |

So a row you do not touch does nothing, and running the same file twice
does nothing the second time. That is what makes the loop safe to repeat
-- export, fill in what you know, apply, and go round again.

A row's shape depends on its population. `unplaceable` and
`guessed_timezone` rows carry `fields`; `unclassified` rows carry a
`collection` to change; `skipped` rows carry both, with the collection
blank because there is no object to read one from. **A `skipped` row
with no collection is passed over, not refused** -- that is its ordinary
state, and refusing it would make every unanswered row an error on every
run.

### Values are checked before they are stored

A correction is laid over its object with pydantic's `model_copy`, which
does not validate. A latitude typed into the JSON as `"35.7720"` would
therefore reach Tripsy as the string it looks like.

So `apply` merges each row into its object, validates the result, and
stores the validated form: `"35.7720"` is kept as the number `35.772`,
and `"not a latitude"` is refused by name rather than discovered halfway
through an upload.

### Applying more than one work-list

Corrections are loaded and merged rather than replaced, so applying a
second work-list does not discard the first. A trip keeps all of its
corrections in one file, so rows are grouped by trip before anything is
written.

The file records the archive it came from, and `apply` refuses one
exported from somewhere else. Applied anyway it would match no uuids and
report that nothing needed doing, which is a true statement and the
wrong answer.

## OPTIONS

`--archive DIRECTORY`
: Directory the staged trips are read from. Defaults to
  `$TRIPSY_EXIM_ARCHIVE`, then `~/.local/share/tripsy-exim/archive`.

`--disagree-km FLOAT`
: `infer` only. Refuse a code whose recorded positions sit further apart
  than this. Default `50.0`. Not the same quantity as
  [fix-locations(1)](fix-locations.md)'s `--far-km`, which measures a
  geocoder's answer against a trip at a much looser 2000.

`--population NAME`
: Report only this kind of gap. Repeatable; the default is all four.
  One of `unclassified`, `guessed_timezone`, `skipped`, `unplaceable`.

`TRIP`
: Trips to act on, by key or by part of a name. Naming none covers the
  whole archive.

`FILE`
: The work-list. `export` writes it, `apply` reads it.

`--write`, `--dry-run`
: `apply` only. Dry run by default, matching [upload(1)](upload.md) and
  [fix-locations(1)](fix-locations.md): nothing is saved until
  `--write`.

## EXAMPLES

What the whole archive still needs:

```sh
uv run tripsy-exim backfill report
```

One trip, by part of its name:

```sh
uv run tripsy-exim backfill report 'Kyoto'
```

Just the things a retype would fix:

```sh
uv run tripsy-exim backfill report --population unclassified
```

The whole loop, in order:

```sh
uv run tripsy-exim backfill infer --write
uv run tripsy-exim backfill report
```

Then the part that needs you. Write the work-list, edit it, see what it
would do, then do it:

```sh
uv run tripsy-exim backfill export work.json
$EDITOR work.json
uv run tripsy-exim backfill apply work.json
uv run tripsy-exim backfill apply work.json --write
```

One trip's placing gaps on their own, which is the usual way in -- the
repeated airports first, since answering one closes every gap naming it:

```sh
uv run tripsy-exim backfill export work.json --population unplaceable 'Kyoto'
```

## NOTES

An archive directory that exists but holds no staged trips is an error
rather than an empty report: having nothing open and having staged
nothing are different answers, and the second is usually the wrong
`--archive`.

`export` refuses to write an empty work-list. A file with no rows is one
you would edit and apply to no effect, so saying "nothing is open" up
front is the better answer.

`infer` reads positions from the whole archive even when you name one
trip, because the airport this trip left unplaced was very likely placed
on another.

## SEE ALSO

[archive(7)](archive.md), [models(7)](models.md),
[fix-locations(1)](fix-locations.md), [upload(1)](upload.md)
