# fake-api(7) -- the in-memory Tripsy API the tests run against

## NAME

fake-api -- what `tests/fake_tripsy.py` models, how far to trust it, and
how to lift it out for another project

## DESCRIPTION

The whole test suite runs against an in-memory model of the Tripsy API
rather than the live service. No test needs credentials, no test touches a
real account, and the suite runs offline in a couple of seconds.

It lives in `tests/fake_tripsy.py` and comes in two halves:

- **`FakeTripsy`** holds the data and the semantics -- trips and their
  children in dictionaries, an id allocator, a clock, deletion tombstones.
- **`transport()`** wraps one in an `httpx.MockTransport`, a thin router
  from method and path onto a store method.

Mounting it is one line, which is the whole of the integration:

```python
client = httpx.Client(base_url=BASE, transport=transport(store))
```

Because the seam is an `httpx` transport rather than a patched function,
the real `TripsyClient` runs unmodified against it -- pacing, retries,
auth headers, pagination and error mapping all execute exactly as they do
against the live service.

## HOW HONEST IT IS

**It is a low-effort fake, and it should be read as one.**

It was built to get the project moving: enough of an API to exercise basic
operations while the client and the importer were being written, without
anybody having to hit Tripsy for every test run. It was never an attempt
to reimplement Tripsy.

What raised it above a guess is the order things happened in. The early
version modelled what the documentation said. Then the project started
making real calls against a real account -- one-off probe scripts were
written for exactly this -- and wherever the live service
turned out to behave differently from the fake, **the fake was corrected
to match the service**. So its semantics are a record of what was actually
observed, not of what was expected.

That gives three tiers of confidence, and they are worth keeping apart:

| Tier | What it means |
|---|---|
| **Verified** | Checked against the live API, on a dated run. The idempotency behaviour, the `updatedSince` cushion, `sort_order` being stored and never computed, v2's page size, the trip identifier length floor. |
| **Documented** | Modelled from Tripsy's published documentation and never observed, because the account cannot produce it. Withholding `price` and `currency` from a caller without expense permission is the case: the account used is premium, so that response can never be seen from outside. |
| **Invented** | Models no observed behaviour at all. The throttling responses are the only ones: probing found no rate-limit headers of any kind and no documented limit, so what Tripsy sends when it has had enough is simply not known. The canned `429` with a `Retry-After` exists to exercise the client's directive handling, and is not a claim about Tripsy. |

Comments in the file mark which is which, with the date a behaviour was
verified. Where a fake is not honest about its own uncertainty it stops
being a test double and becomes a second source of truth, which is the
failure mode this table exists to prevent.

## WHAT IT MODELS

Enough of the API for an import and a read-back to run end to end:

- **Both versions.** v1 for writes and the bare `results` trip list, v2
  for reads, pagination and deletion reporting.
- **Idempotency.** A create whose `internal_identifier` the store already
  holds answers an empty `200` and creates nothing -- including the
  five-character floor below which trip-level suppression does not engage.
- **Deletion tombstones.** A deleted object stays deleted and its
  identifier stays spent, which is the behaviour the whole generation
  mechanism exists to work around.
- **Pagination** at 100 results, the real page size. A real exported trip
  carries 121 events, so the second page is reached on the first import
  rather than hypothetically.
- **`updatedSince`**, including the two-day cushion the server subtracts
  before filtering.
- **Field selection** -- `fields=` and `exclude=`.
- **Partial responses**, via `can_see_expenses=False`.

And three things a real service will not do on request, which is the
point of a fake:

- `advance(days=3)` moves the store's clock, so `updatedSince` tests are
  deterministic rather than dependent on how long the suite took.
- `latency` charges seconds to a `FakeClock` the pacer reads, so a test
  can make the API appear slow without the suite running slowly.
- `throttle()` and `lose_response()` queue canned failures, so retry and
  backoff paths are exercised on demand.

The two clocks are deliberately separate and must not be conflated: `now`
is the store's own wall clock, and `clock` is the monotonic clock the
pacer reads.

## LIFTING IT OUT FOR YOUR OWN PROJECT

`tests/fake_tripsy.py` has no dependency on anything in `tripsy_exim`. It
imports `json`, `re`, `datetime`, `httpx`, and `tests/clock.py` for the
fake monotonic clock. So taking it is genuinely two files:

```sh
cp tests/fake_tripsy.py tests/clock.py your_project/tests/
```

Then drop the `FakeClock` import if you do not want the pacing hooks, and
mount the transport on whatever client you use. It carries this project's
licence -- see [LICENSE](../LICENSE).

**Three warnings, and take them seriously.**

1. **It is low effort by design.** It models what this project needed for
   an import and a read-back. Anything outside that -- the parts of the
   API `tripsy-exim` never calls -- is not modelled, not tested, and in
   several cases not even routed.
2. **It is a snapshot, not a contract.** Its behaviour was observed on
   dated runs against one account in 2026. **Tripsy can change their API
   whenever they like, and nothing here will notice.** A fake that was
   accurate when it was written and is never re-checked becomes a
   confident description of a service that no longer exists -- and your
   tests will keep passing while production breaks.
3. **One account is not the API.** Everything verified was verified
   against a premium account with full permissions. Behaviour for other
   account types is documented at best and guessed at worst.

If you do take it, take the probe habit with it. The value here is not the
822 lines of Python -- it is that somebody ran the real calls and wrote
down what came back. Re-run that against the live service periodically and
the fake stays worth having; skip it and the code is a liability wearing a
test double's clothes.

## SEE ALSO

[models(7)](models.md), [archive(7)](archive.md), the
[docs README](README.md)
