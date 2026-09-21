# backfill(1) -- author corrections against what the archive knows

## SYNOPSIS

```text
tripsy-exim backfill report [--archive DIRECTORY]
                            [--population NAME]... [TRIP]...
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

Only `report` exists so far. It writes nothing.

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

## OPTIONS

`--archive DIRECTORY`
: Directory the staged trips are read from. Defaults to
  `$TRIPSY_EXIM_ARCHIVE`, then `~/.local/share/tripsy-exim/archive`.

`--population NAME`
: Report only this kind of gap. Repeatable; the default is all four.
  One of `unclassified`, `guessed_timezone`, `skipped`, `unplaceable`.

`TRIP`
: Trips to report on, by key or by part of a name. Naming none reports
  the whole archive.

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

## NOTES

An archive directory that exists but holds no staged trips is an error
rather than an empty report: having nothing open and having staged
nothing are different answers, and the second is usually the wrong
`--archive`.

`backfill infer`, `backfill export` and `backfill apply` are not written
yet. When they are, `infer` runs first -- it fills what the archive can
work out from itself, with no human input, so the list this command
prints is shorter by the time a person reads it.

## SEE ALSO

[archive(7)](archive.md), [models(7)](models.md),
[fix-locations(1)](fix-locations.md), [upload(1)](upload.md)
