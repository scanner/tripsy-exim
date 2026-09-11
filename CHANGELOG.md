# Changelog

All notable changes to this project will be documented in this file.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial project scaffolding.
- Canonical trip models: `Trip`, `Hosting`, `Activity`, `Transportation`,
  `Expense`, and `Collaborator`.
- Local archive of canonical trips on disk, with a sync manifest and an
  export watermark.  Writes merge on set fields, so a partial export
  cannot erase data an earlier one captured.
- Deterministic `internal_identifier` minting, so re-running an import
  updates rather than duplicates.
- Survive changes to Tripsy's own payloads: a new field is kept, and a
  payload that no longer parses is saved verbatim under `quarantine/`
  instead of failing the run.  Undocumented fields seen during a run
  are reported.
- Tripsy API client covering the v1 write routes, the v2 paginated read
  routes, and the `POST /auth` login exchange.  Duplicate creates report
  that nothing was written rather than looking like a success.
- Self-imposed rate limiting on every call.  Tripsy publishes no limit and
  sends no rate-limit headers, so the pace is derived from how long each
  response takes: a fast API is still paced, a slowing one is backed away
  from, and a `Retry-After` -- should one ever arrive -- sets the floor for
  the rest of the run.
- Pacing profiles for the three ways this gets used: `import` for a bulk
  load, `backup` for a scheduled export, `interactive` for a handful of
  calls with someone waiting.  Each sets its own request timeout, since
  how long to hang on for is the same judgement as how fast to go.
- Parse a TripIt-exported `.ics` file into a trip and its child objects.
  The export carries no timezone and no type information, so zones are
  derived from each event's coordinates and what an event *is* is inferred
  from its wording.
- Every parsed event comes with a note saying what was decided and why.
  Events that matched no rule become activities and are listed, so a
  misfiled event can be caught before an import writes it.
- Report a duplicate create accurately: suppression covers one collection
  of one trip, and a deleted trip keeps its identifier, so neither a
  retyped object nor a deleted trip can be fixed by re-running an import.
