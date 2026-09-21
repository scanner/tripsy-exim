# Importing your TripIt trips into Tripsy

A step-by-step guide. Start at the top and work down -- each step says
what to expect before you run the next one.

Everything up to step 7 happens on your own disk. Nothing is sent to
Tripsy, nothing needs your password, and you can throw it all away and
start again as many times as you like. Only the upload is permanent, and
it has a rehearsal.

Budget an evening for a long history: most of it is waiting for TripIt to
send you the file, and then looking at trips in the app as they arrive.

## What you need

- **This project, and `uv`.** Every command below is written as `uv run
  tripsy-exim`, which works from a fresh clone with no setup. If you would
  rather type `tripsy-exim`, [the docs README](README.md) has the three
  ways to drop the prefix.
- **Your TripIt data.** Step 1 gets it. It takes a few days.
- **A Tripsy account**, with the username and password you sign in with.
  You do not need them until step 7.
- **Disk space.** A long history is tens of megabytes of JSON.

## How it works

Your data goes into a local **archive** first, in this project's own
format. You review and correct it there, for free, as many times as you
want. Only when you are happy does any of it go to Tripsy.

The archive sits in the middle on purpose: everything before the upload
is reversible and costs nothing, and the upload is a one-way door. The
README has [the longer version of why](../README.md#workflows).

## 1. Get your TripIt data

TripIt does not offer this from the app. Email `support@tripit.com`, from
the address you sign in to TripIt with, and ask for a:

> GDPR Request - complete JSON export of my personal account data

In practice the reply arrives within a couple of days, with the JSON
**attached to the email**. Save the attachment somewhere you can find
it.

The file is ordinary JSON with nothing GDPR-specific in it -- phrasing
the request that way is just what gets a complete export rather than a
partial one, which is why everything here simply calls it the TripIt
JSON export.

## 2. Stage it

```sh
uv run tripsy-exim stage-export ~/Downloads/tripit-export/export.json
uv run tripsy-exim list
```

`list` should now show every trip in your history, oldest first, with a
blank mark in the first column meaning "not uploaded".

This reads the file and writes canonical objects to
`~/.local/share/tripsy-exim/archive` (or `$TRIPSY_EXIM_ARCHIVE`). It
touches no network. Re-running it is always safe: the same source
produces the same objects, so a second run rewrites rather than
duplicates.

If you also have per-trip `.ics` files, stage them *after* the export
with [stage(1)](stage.md) -- the export owns a trip's identity, so a
calendar for a trip already staged is refused rather than staged twice.

### One journey that arrived as two trips

The export records a trip per traveller, so a holiday taken together
appears twice, each copy holding that person's own flights and room.
Uploading both would make two rival trips out of one journey. Declare one
absorbed by the other:

```sh
uv run tripsy-exim merge 'Kyoto, June 2024'
```

Nothing moves on disk and the declaration can be undone --
[merge(1)](merge.md) has the details.

## 3. Let the archive fix what it can

```sh
uv run tripsy-exim backfill infer
uv run tripsy-exim backfill infer --write
```

Some endpoints arrive naming an airport by its code with nothing to put
on a map. Usually another trip in your own history placed that same
airport, so the answer is already on your disk. This finds those and
fills them in.

The first run shows you what it would do; `--write` does it. It refuses
anything it is not sure of, including codes like `TYO` that name a city
rather than one airport.

## 4. See what still needs you

```sh
uv run tripsy-exim backfill report
```

This writes nothing. It groups what is still open, and lists places
**commonest first** -- one station recurs across a whole history, so
answering it once closes every gap that names it. If it says `nothing
open`, skip to step 6.

Four kinds of thing can be open; [backfill(1)](backfill.md) explains each
if you want more than the one-line version.

## 5. Answer it

```sh
uv run tripsy-exim backfill draft work.json
```

Open `work.json` in whatever you like -- a text editor, or a tool built
for JSON such as [fx](https://fx.wtf). Each row is one question,
pre-filled with whatever the object holds now:

```json
{
  "trip": "Osaka, June 2012",
  "trip_key": "txim-tripit-json-g01-7748bce7...",
  "uuid": "878a0a04-2279-5b88-9192-27ec4fb9be88",
  "identifier": "txim-tripit-json-g01-ec060d2e...",
  "population": "unplaceable",
  "object": "VAN (arrival)",
  "fields": {
    "arrival_address": "",
    "arrival_latitude": "",
    "arrival_longitude": ""
  }
}
```

`object` tells you what the row is about -- here, where a flight landed.
Edit **`fields`** and leave the rest alone: they are how the row finds
its way back to the right object.

Fill in what you know. An address alone is enough -- Tripsy geocodes it
for you, and is better at it than a borrowed coordinate would be.

**A row you leave alone does nothing.** You do not have to answer
everything, or anything. Then:

```sh
uv run tripsy-exim backfill apply work.json
uv run tripsy-exim backfill apply work.json --write
```

Dry run first, as always. Only what you actually changed is written, so
running the same file twice does nothing the second time, and you can go
round steps 4 and 5 as many times as you like.

## 6. Give it your Tripsy credentials

The upload is the first step that needs them. The simplest way is a
`.env` file in the directory you are working from:

```sh
TRIPSY_USERNAME=you@example.com
TRIPSY_PASSWORD=...
```

`--username` and `--password` work too, and if you keep secrets in
1Password there is `TRIPSY_SECRET_URL` -- see
[CREDENTIALS in the README](../README.md#credentials) for the full
resolution order and what is stored where.

## 7. Upload, a trip at a time

```sh
uv run tripsy-exim upload --limit 1 --verbose
uv run tripsy-exim upload --limit 1 --write
uv run tripsy-exim verify --limit 1
```

The first command plans and prints without sending anything -- that is
the default, and `--verbose` lists every object it would create. The
second sends it. `verify` reads the trip back from Tripsy and compares it
to what was meant to be there.

Then **open the trip in the Tripsy app and look at it.** That is the
whole reason for `--limit 1`: see one trip arrive, decide you are happy
with the shape of it, and only then do the rest.

```sh
uv run tripsy-exim upload --limit 10 --write
```

Re-running is safe. A trip an earlier run finished is skipped, and an
object Tripsy has already seen is not created twice.

### Rehearsing first

If you would rather see a whole run land before committing to it, stage a
second copy under a throwaway namespace:

```sh
uv run tripsy-exim stage-export --scratch ~/Downloads/.../export.json
```

Upload that, look at it, delete the trips in the app, and do the real run
afterwards. It spends identifiers the real import will never want, so
nothing is lost -- [models(7)](models.md) explains why that matters.

## 8. Place what is left

```sh
uv run tripsy-exim fix-locations --trip 'Kyoto'
uv run tripsy-exim fix-locations --trip 'Kyoto' --write
```

Tripsy places an activity's address itself when you open the trip, so do
this *after* looking at your trips -- there will be less to do. What
remains is mostly transportation endpoints, which the app never geocodes.

This one asks an outside geocoding service, and refuses answers that land
implausibly far from the rest of the trip.
[fix-locations(1)](fix-locations.md) covers choosing a service and
correcting an answer that was believed and should not have been.

## A worked example

Two trips, one of which flew through an airport the other one placed.
This is a real run, produced by the test suite walking this guide --
`tests/test_importing_guide.py` -- so it cannot drift away from what the
commands actually print.

Steps 1 and 6 are missing because they cannot be automated: one is an
email to TripIt, the other is your own credentials.

```console
$ tripsy-exim stage-export export.json
  txim-tripit-json-g01-2440a45af136ce56 (1 hostings, 1 transportations)
  txim-tripit-json-g01-7748bce735ae9e27 (1 activities, 1 transportations)

2 trips, 4 objects into archive
  1 activities, 1 hostings, 2 transportations

$ tripsy-exim list
      2012-06-01  Osaka, June 2012                                 txim-tripit-json-g01-7748bce735ae9e27
      2024-05-01  Kyoto, May 2011                                  txim-tripit-json-g01-2440a45af136ce56

2 trips, 0 already uploaded

$ tripsy-exim backfill infer
  NAR    35.7720,  140.3929  1 agreed   txim-tripit-json
  refused  VAN (arrival): nothing else in the archive places this

1 would be placed, 1 refused

Nothing was saved.  Pass --write to save it.

$ tripsy-exim backfill infer --write
  NAR    35.7720,  140.3929  1 agreed   txim-tripit-json
  refused  VAN (arrival): nothing else in the archive places this

1 placed, 1 refused

$ tripsy-exim backfill report

Osaka, June 2012
  unplaceable           1  correct

by place, commonest first:
     1x  VAN                                          arrival

1 gap across 2 trips

$ tripsy-exim backfill draft work.json
1 rows written to work.json
Edit it, then: tripsy-exim backfill apply work.json --write

$ tripsy-exim backfill apply work.json

1 would be written -- 1 corrected, 0 retyped, 0 added
0 already correct, 0 left blank, 0 refused

Nothing was saved.  Pass --write to save it.

$ tripsy-exim backfill apply work.json --write

1 written -- 1 corrected, 0 retyped, 0 added
0 already correct, 0 left blank, 0 refused

$ tripsy-exim backfill report
2 trips, nothing open

$ tripsy-exim upload --limit 1

Osaka, June 2012  [txim-tripit-json-g01-7748bce735ae9e27]
  2 objects

1 trip, 2 objects, 0 untyped legs

Dry run.  Nothing was sent.  Pass --write to upload.

$ tripsy-exim upload --limit 1 --write

Osaka, June 2012  [txim-tripit-json-g01-7748bce735ae9e27]
  2 objects

1 trip, 2 objects, 0 untyped legs
Osaka, June 2012                         created  +2 =0

2 created, 0 already there

$ tripsy-exim verify --limit 1

Osaka, June 2012  [ok]  2 of 2 planned
    2 addresses not yet placed

1 of 1 trips match their plan
2 addresses carry no position.  An activity may place itself once the app renders the trip; a leg never will.  `fix-locations` places the rest.
```

Note what happened at each turn: `infer` placed NAR because the other
trip had placed it, and refused VAN because nothing had. `report` then
listed only VAN. One line filled into the work-list closed it, and the
second `report` found nothing open. Only then did anything reach Tripsy,
one trip at a time, dry run before each write.

## If something looks wrong

**A trip is missing.** Check `list` -- if it is marked `->` it was
declared as merged into another. `merge --undo` reverses that.

**A trip looks wrong in the archive.** Fix it with steps 4 and 5 and
upload again; corrections are applied on the way out, so re-staging never
clobbers them.

**A trip looks wrong in Tripsy, after uploading.** Edit it in the app.
That is not a failure -- the goal here is a good *initial* state, not a
perfect one, and some things are quicker to fix by hand.

**Something was uploaded that should not have been.** Delete it in the
app. Be aware that re-uploading the same record afterwards will not
recreate it -- see below.

## The one thing you cannot undo

Tripsy never releases an identifier. Deleting an object in the app does
not free the identifier it used, so re-importing that record creates
nothing at all.

That is the whole reason this project puts a local archive in the middle:
every correction is free until you upload, and permanent after. Take the
passes you need at steps 4 and 5, use `--limit 1` for the first trip, and
rehearse with `--scratch` if you want to see it all land first.

If you do need a record back after deleting it, [Identifiers in the
README](../README.md#identifiers-and-why-imports-are-idempotent) explains
the generation counter, which is the way out.
