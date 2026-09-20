# models(7) -- the canonical object model

## NAME

models -- the pydantic objects `tripsy-exim` holds trips in, and how they
map to Tripsy's

## DESCRIPTION

`tripsy_exim.models` is the hinge of the project. Parsers in `sources/`
produce these objects, `sync/` decides what to do with them, `api/` sends
them to Tripsy, and `store/` writes them to disk. Nothing else in the
package is shared by all four layers.

They are pydantic models, one module per object, all built on
`CanonicalModel` in `models/base.py`.

## THE MAPPING TO TRIPSY

**The object set is one to one. The field set is a superset.**

That is the whole of it, and the two halves are worth stating separately
because they are not the same claim.

### One object, one model

Six models, six kinds of thing in a Tripsy account:

| Model            | Tripsy object                 | Module                     |
|------------------|-------------------------------|----------------------------|
| `Trip`           | a trip                        | `models/trip.py`           |
| `Activity`       | an activity on a trip         | `models/activity.py`       |
| `Hosting`        | a stay on a trip              | `models/hosting.py`        |
| `Transportation` | a leg on a trip               | `models/transportation.py` |
| `Expense`        | an expense on a trip          | `models/expense.py`        |
| `Collaborator`   | someone with access to a trip | `models/collaborator.py`   |

There is no model that is two Tripsy objects, and no Tripsy object that is
two models. A field named `latitude` in Tripsy is named `latitude` here.
Nothing is renamed on the way in or the way out, so a payload read from
the API and the JSON file the archive writes for it are recognisably the
same document.

`Trip` is the root: every other object except `Collaborator` carries a
`trip` field holding the numeric id of the trip it belongs to.

### One model, more fields than Tripsy has room for

The models are provider-neutral. Tripsy is the first service they are
mapped to, not the shape they were derived from, and two mechanisms let
them hold more than Tripsy does:

- **Source fields Tripsy cannot store** are kept under the `x_source` key
  (`SOURCE_KEY`), written by `with_source()` and read by
  `source_extras`. A TripIt record carries things Tripsy has nowhere to
  put; they are retained rather than dropped, so an import through this
  model loses nothing even where Tripsy's schema is narrower.
- **Fields Tripsy returns that are not declared** land in the same
  passthrough container, readable through `wire_extras`. `model_config`
  sets `extra="allow"` for exactly this: an undocumented field, or one
  Tripsy adds later, is archived intact instead of rejected.

The two are kept apart by the `x_source` namespace -- `wire_extras`
excludes it, `source_extras` is only it -- so "what Tripsy said" and
"what the source said" never blur into each other.

### WRITABLE: the projection back down

A payload built by dumping a whole object would carry read-only fields
Tripsy never accepts back, and would be rejected or, worse, quietly
misinterpreted. So each model declares a `WRITABLE` class variable naming
the fields its own endpoint accepts, and `writable_payload()` is the only
thing that builds a request body:

```python
payload = activity.writable_payload()
```

It dumps only fields that were explicitly set, restricts them to
`WRITABLE`, and converts `Money` back to a JSON number. A PATCH built this
way touches nothing the caller did not set.

`update_trip` is deliberately absent from every model's `WRITABLE`. On a
child PUT or PATCH that field moves the object to a different trip, which
is never something to carry along from a fetched payload -- `api.client`
has `move_child()` for when it is meant.

`Collaborator.WRITABLE` is empty: the API exposes no way to create or
modify one, so the model exists to be read and archived.

## WHERE THE SHAPES ARE NOT UNIFORM

Tripsy is not uniform across its own endpoints, and the models record that
rather than papering over it. Four cases to know:

`internal_identifier`
: Present on `Trip`, `Activity`, `Hosting` and `Transportation`. Absent
  on `Expense` and `Collaborator`. Expenses therefore get none of the
  idempotency every other object has for free, and an importer has to
  match on the fields themselves or track what it wrote. The question
  does not arise for a collaborator, which the API offers no way to
  create at all. See IDENTITY below.

`owner`
: Two shapes. `Trip` and `Expense` return a bare user id, so `owner` is
  an `int`. `Activity`, `Hosting` and `Transportation` return an object,
  so `owner` is an `ObjectOwner`. Both are kept as the endpoint returns
  them rather than normalised into one shape.

dates versus datetimes
: `Trip.starts_at` and `Trip.ends_at` are plain `date`. The same field
  names on every child object are `UtcDatetime`. The API uses both and
  they are not interchangeable.

`arrival_apple_maps_id`
: Returned by v2 on a transportation but not in the documented writable
  set, unlike `departure_apple_maps_id`. Declared, and read-only.

`Trip` also carries the count of collaborators under two names --
`collaborators` in v2, `collaborators_count` in v1 -- and neither is the
collaborator list, which comes from its own endpoint.

## IDENTITY

**The field is Tripsy's. The value is ours.**

`internal_identifier` is declared by Tripsy, on a trip, activity, hosting
or transportation. What Tripsy does *not* do is fill it in -- it hands the
field to the client and lets the client decide what goes there, on two
conditions that are never stated as rules but behave as though they were:

1. **The value is unique** across the objects you set it on.
2. **The value is permanent.** Once Tripsy has seen it, it is spent.
   Deleting the object does not give it back.

In return Tripsy treats the field as an **idempotency key**: POSTing an
object whose identifier it already holds answers an empty `200` rather
than creating a second object.

So the field is a collaboration. Tripsy provides the slot and the
guarantee; the client provides a naming scheme it can live with
permanently. `tripsy-exim` derives its values from the source record
rather than generating them randomly, so the same record always produces
the same identifier across runs, machines, and rebuilds of the archive.
That is what turns a re-run of an import into a no-op instead of a pile of
duplicates, and what makes a half-finished run resumable by simply running
it again.

`models/identifiers.py` holds the minting. See IDENTIFIERS in
[archive(7)](archive.md) for the anatomy of a minted identifier and what
the generation segment is for.

One limit belongs here rather than there, because it is a property of
Tripsy's field rather than of our scheme: trip-level duplicate suppression
only engages above five characters. Child objects suppress at any length.
`mint()` never produces anything that short, so this bounds what a
hand-written identifier may be, not what the package does.

### Scratch identifiers, and why the project needed them

Permanence is a pleasant property in production and an obstacle
everywhere else. Developing the upload and update paths means running them
over and over against a real account -- and every run of a create spends
identifiers that can never be reclaimed. Uploading a trip to see what
arrives, deleting it because it arrived wrong, fixing the code and
uploading again does not work: the second upload creates nothing, because
Tripsy still remembers the identifiers from the first.

The way out is to spend identifiers that the real import will never want.
A scratch run mints into a throwaway namespace:

```text
txim-scratch-4f2a1c8e-g01-52b4a7ef87e23b68
 |    |       |
 |    |       `-- a token unique to this run
 |    `---------- the scratch marker
 `--------------- the same source record, the same digest
```

The digest is unchanged, because it is still the same source record. Only
the key space differs. That gives a full-fidelity rehearsal -- the same
objects, the same ordering, the same payloads, against the live service --
whose entire output can be deleted afterwards without having cost the real
import anything.

`--scratch` on [stage(1)](stage.md) or
[stage-export(1)](stage-export.md) mints a fresh namespace per run and
prints it. `is_scratch()` is what keeps the two key spaces from
interfering:

- A scratch trip does not block the real one. Staging normally refuses a
  trip the archive already holds, and scratch trips are exempt in both
  directions -- staging the same source as a rehearsal and then for real
  is the expected sequence, not a duplicate.
- A scratch trip is not counted when resolving which archived trips a
  source matches, so a rehearsal left lying around does not confuse a
  later merge or import.

This was a key element in testing the upload and update paths, and it is
the only honest way to exercise them: a fake API can tell you the client
is correct, but only a real account can tell you Tripsy accepts what the
client sends. See [fake-api(7)](fake-api.md) for the other half of that.

**Nothing staged with `--scratch` is the real archive.** A rehearsal is
data you intend to throw away, and the namespace is there to make throwing
it away free.

## TYPES

`UtcDatetime`
: An aware `datetime`, normalised to UTC. A naive one is rejected at
  validation rather than assumed to be anything. The local wall-clock
  time a traveller actually read is recoverable from the instant plus the
  object's own timezone field -- which is why a transportation carries
  `departure_timezone` and `arrival_timezone` separately.

`Money`
: A `Decimal`. The API returns a JSON float, which is routed through
  `str` on the way in so that `78.5` does not become `78.5000000001`, and
  converted back to a float at the boundary by `writable_payload()`.
  Money is `Decimal` in memory and on disk, and a float only on the wire.
  The fields this applies to are named in `MONEY_FIELDS`.

Free strings where a value set is undocumented
: `activity_type`, `period`, `transportation_type`, `seat_class` and the
  endpoint `location_type` fields are all `str | None` rather than enums.
  The API documents no value set for any of them and only a handful of
  values have been observed, so constraining them would turn an
  unanticipated value into a failed import.

`sort_order`
: One dense sequence across a whole trip rather than per collection or
  per day -- activities, hostings and transportations share it. The API
  stores what it is given and computes nothing, so an object created
  without one holds `0`. The importer numbers a trip chronologically.
  See SORT ORDER in [archive(7)](archive.md) for why it cannot be
  revisited.

## MERGING

`merged_with()` overlays a newer, possibly partial, version of an object
onto an older one.

It exists because any Tripsy response can be partial. `price` and
`currency` are withheld entirely from a caller who cannot see expenses,
and a `fields=` response is partial by construction. A blind overwrite
would let one restricted read erase what a fuller one captured.

The rule is that **only fields the incoming object actually set
participate**. A field set to `null` does overwrite, because null is a
value; a field that is simply absent does not, because absence is not.
This is what `exclude_unset` buys, and it is why the models are careful to
keep unset distinguishable from null throughout.

Source fields accumulate across a merge rather than being replaced, so a
later read that knew about fewer of them does not drop the rest.

Every archive write goes through this, so a file on disk holds the union
of everything ever seen about that object.

## WHEN A PAYLOAD WILL NOT PARSE

Tripsy can change the type of a field it already returns, and pydantic
rejects the whole object when it does. One changed field would otherwise
cost every other field on that object and abort the run that was supposed
to be protecting the data.

So a payload that will not validate is written verbatim under
`quarantine/` in the archive and the run continues. The cost is that one
object's typed access is lost for that run, and nothing more.

## SEE ALSO

[archive(7)](archive.md), [fake-api(7)](fake-api.md), the
[docs README](README.md)
