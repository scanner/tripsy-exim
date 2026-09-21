# fix-locations(1) -- geocode the objects Tripsy left without a position

## SYNOPSIS

```text
tripsy-exim fix-locations [--archive NAME] [--trip TEXT]...
                          [--geocoder TEXT] [--api-key TEXT]
                          [--cache FILE] [--far-km FLOAT]
                          [--forget TEXT]... [--write | --dry-run]
                          [--username TEXT] [--password TEXT]
```

## RUNNING

Examples below are written as `uv run tripsy-exim`, which is how the
command runs from a fresh clone. See [the docs README](README.md) for
when you can drop the `uv run`.

## DESCRIPTION

A clean-up run over trips already uploaded. It finds every object in
Tripsy carrying an address and no coordinates, looks that address up with
an outside geocoding service, and sends the coordinates back.

The name says what it fixes and not how, so: **it geocodes.** It does not
correct addresses, rename anything, or reconcile the archive against the
account -- that is [verify(1)](verify.md). It puts a pin on the map where
there was only text.

### Why anything needs fixing

Tripsy's server geocodes nothing. The app geocodes an activity's address
when it renders it and writes the result back; a transportation endpoint
is never geocoded at all, on create or on update. So an object pins only
if something hands it coordinates.

That splits the work in three:

1. Coordinates the archive already has go up with the create, in
   [upload(1)](upload.md). Most flights are in this group -- an airport
   endpoint usually arrives from the export already placed.
2. Activities and hostings the app has resolved need nothing. Opening a
   trip in Tripsy is what resolves them, and this command skips whatever
   is already placed, so running it after looking at a trip leaves less
   to do.
3. Everything else is this command: every transportation endpoint the
   export had no coordinates for, and whatever the app could not resolve
   either.

It reads Tripsy rather than the archive, because what wants placing is
whatever the app has *not* placed, and only Tripsy knows that. Nothing is
written back to the archive either: the archive holds what the source
gave us, and this is a clean-up over what was imported.

### What it refuses

A geocoder does not fail by returning nothing. It fails by confidently
returning somewhere: `Kyoto Station` resolves to a point in El Dorado
County, California, about 8,900 km from Kyoto, and nothing about that
answer looks wrong on its own.

So every answer is measured against the positions its trip already holds,
and one landing further than `--far-km` away is refused rather than
written. A result matched as an administrative boundary is reported as
coarse -- a town that answers with the county it sits in is not a wrong
answer so much as an answer to a different question.

Addresses are normalised before they are asked about: a trailing phone
number, a ZIP+4 written without its hyphen, and runs of commas left by
empty fields all make an otherwise good address resolve to nothing. Only
things that were never part of the address are removed.

### The cache

Every answer is kept, including the ones that found nothing, in
`~/.config/tripsy-exim/geocode.json`.

This is a condition of use rather than an optimisation. Nominatim's
usage policy states that results must be cached on the client side, and
that clients repeating the same query may be blocked. That is also why
misses are remembered: asking again about an address that has no answer
is exactly the behaviour the policy warns about. It lives in durable
config rather than a cache directory for the same reason -- a disk
cleaner should not be able to remove something compliance rests on.

Requests are made from one thread at a little over one second apart, with
a User-Agent naming this application, as the same policy requires.

## OPTIONS

`--write`
: Actually send the positions to Tripsy.

`--dry-run`
: Report what would be written, and write nothing. The default. Nothing
  is asked of the geocoder either -- a lookup is a request to an outside
  service under a policy that counts them, so a plan does not make one.
  Addresses the cache already answers are shown as `would place`, with
  the position and the label; the rest are shown as `would look up`.

`--trip TEXT`
: Fix only this trip, named by part of its name or by its key.
  Repeatable. The default is every trip an earlier run uploaded.

`--geocoder TEXT`
: Which `geopy` service to ask. Default `nominatim`, which needs no
  account. Any service `geopy` knows will do -- the service is a choice
  that may change, which is why the lookup goes through `geopy` at all.

`--api-key TEXT`
: Key for a geocoder that wants one, e.g. `opencage`.

`--cache FILE`
: Where answers are kept between runs. Defaults to
  `$TRIPSY_EXIM_GEOCODE_CACHE`, then
  `~/.config/tripsy-exim/geocode.json`.

`--far-km FLOAT`
: Refuse a result this many kilometres from everything else on its trip.
  Default `2000.0`. A leg of a road trip can legitimately be several
  hundred kilometres out; a geocoder's confident mistake is usually
  thousands, so the threshold does not have to be clever. A trip with
  nothing placed has nothing to measure against, and its results are
  believed.

`--forget TEXT`
: Drop this address from the cache and place it again, even where it
  already has a position. Repeatable. This is the way to correct an
  answer that was believed and should not have been -- the cache does not
  consider it wrong, so nothing else would ever replace it. Give the
  address as Tripsy holds it. On a dry run the cache file is left alone
  and the report says `would be forgotten`.

`--username TEXT`, `--password TEXT`
: Credentials for this run. See CREDENTIALS in the
  [README](../README.md).

`--archive NAME`
: Staging archive `--trip` is resolved against. Named under `<root>/staged/`.
  Defaults to `staged`.

`--archive-root DIRECTORY`
: Where the staging archives and the exports live. Defaults to
  `$TRIPSY_EXIM_ARCHIVE`, then `~/.local/share/tripsy-exim`.

## EXAMPLES

Look at a trip in the app first, so the app places what it can, then see
what is left:

```sh
uv run tripsy-exim fix-locations --trip 'Lakeside'
uv run tripsy-exim fix-locations --trip 'Lakeside' --write
```

A car-rental desk that resolved to the coffee shop at the same address:

```sh
uv run tripsy-exim fix-locations --trip 'Lakeside' \
    --forget '1 Example Plaza, Springfield, IL 62701, United States' \
    --write
```

Use a different service for a run:

```sh
uv run tripsy-exim fix-locations --geocoder opencage --api-key "$OPENCAGE_KEY"
```

## ENVIRONMENT

`TRIPSY_EXIM_GEOCODE_CACHE`
: Where answers are kept, overriding the default path.

## NOTES

An address that resolves to nothing three times will resolve to nothing
the fourth. When the service genuinely has no record of a place, the fix
is a better address -- correct it in the archive with an override and
upload again, or set the position by hand in the app.

## SEE ALSO

[upload(1)](upload.md), [verify(1)](verify.md), [archive(7)](archive.md)
