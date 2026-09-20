# Documentation

Reference pages for `tripsy-exim`. One page per command, plus two on the
things every command shares: the object model the data is held in, and
the archive it is held on disk.

They are written as man pages -- SYNOPSIS, DESCRIPTION, OPTIONS,
EXAMPLES -- and the section number in each title says which kind it is.
A `(1)` is a command you run. A `(7)` is a concept several commands
share, which is why it has no synopsis of its own.

| Page | |
|---|---|
| [stage(1)](stage.md) | Parse `.ics` files into the archive |
| [stage-export(1)](stage-export.md) | Parse a TripIt JSON export into the archive |
| [list(1)](list.md) | List the staged trips and what has been uploaded |
| [merge(1)](merge.md) | Upload one staged trip as part of another |
| [upload(1)](upload.md) | Send staged trips to Tripsy |
| [verify(1)](verify.md) | Read them back and compare against the plan |
| [fix-locations(1)](fix-locations.md) | Geocode what Tripsy left without a position |
| [models(7)](models.md) | The pydantic object model, and how it maps to Tripsy's |
| [fake-api(7)](fake-api.md) | The in-memory Tripsy API the tests run against |
| [archive(7)](archive.md) | The archive on disk, identifiers, overrides, the manifest |

## Running the commands

**Every example in these pages is written as `uv run tripsy-exim ...`.**

This project is managed with [uv](https://docs.astral.sh/uv/), and the
command lives in the project's virtual environment rather than on your
`PATH`. Running it any other way, from a fresh clone, gets you
`command not found` -- so the examples show the form that works without
any further setup:

```sh
uv run tripsy-exim list
```

`uv run` is a launcher, not part of the command. The SYNOPSIS at the top
of each page therefore shows the bare command, because that is the
program's own grammar and the options belong to it, not to `uv`.

### When you can drop the `uv run`

Three ways, any of which puts `tripsy-exim` on your `PATH` as a plain
command:

1. **Activate the environment** `uv sync` already built. Good for a
   session of several commands:

   ```sh
   source .venv/bin/activate
   tripsy-exim list
   ```

2. **Install it as a tool**, which puts it on your `PATH` for every
   shell, from anywhere:

   ```sh
   uv tool install .
   tripsy-exim list
   ```

3. **Install it into an environment you manage yourself**, if you would
   rather not use uv at all:

   ```sh
   pip install .
   ```

Nothing else changes with any of them. In particular, `.env` is searched
for from the working directory upwards in every case, so credentials
resolve identically whichever way you launch. See CREDENTIALS in the
[README](../README.md).

## What these pages do not cover

A man page documents its own command. Four things are deliberately
elsewhere, because they are about the project rather than about any one
command:

- **Why the project exists**, what state it is in, and what it cannot yet
  do: the [README](../README.md).
- **How the code is arranged**, and the design rules the layout rests on:
  the README's own repository layout section.
- **How to work on it** -- tests, linters, and how the suite's data is
  generated: the README's development section.
- **What has shipped**: [CHANGELOG.md](../CHANGELOG.md).

## Where to start

Reading in this order builds up the way the project does:

1. [models(7)](models.md) -- what a trip is, once it is out of whatever
   format it arrived in.
2. [archive(7)](archive.md) -- where those objects live, how they are
   named, and how a correction is kept separate from what a parser
   inferred.
3. [stage-export(1)](stage-export.md) or [stage(1)](stage.md) -- getting
   data in.
4. [upload(1)](upload.md) and [verify(1)](verify.md) -- getting it to
   Tripsy and checking that it arrived.

The two `(7)` pages are worth the detour before the first upload. An
identifier Tripsy has seen is never released, so the cost of
misunderstanding one is permanent.
