# Changelog

All notable changes to this project will be documented in this file.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing has been released yet; what the project can currently do is
described in [README.md](README.md).

### Added

- Staging archives are named. `--archive NAME` picks which one a command
  works on, defaulting to `staged`, so a scratch archive and a real
  import can sit side by side.
- A step-by-step guide to moving a TripIt account into Tripsy, in
  [docs/importing.md](docs/importing.md), with a worked example.
- `backfill report` says what the staged trips still need a person for,
  with the places listed commonest first.
- `backfill draft` writes an editable work-list of what is open, and
  `backfill apply` reads it back as corrections.
- `backfill infer` places an endpoint from an airport code the archive
  already positions elsewhere, refusing a code that names two places
  -- two airports serving one city, say, or a code like `TYO` that
  names a metropolitan area rather than an airport.

### Changed

- `$TRIPSY_EXIM_ARCHIVE` and `--archive-root` name the directory holding
  every archive, not one archive. A staging archive now lives at
  `<root>/staged/<name>/` and exports will land under `<root>/exports/`.
  **An existing archive has to be moved** into place before any command
  will find it:

  ```sh
  ROOT="$TRIPSY_EXIM_ARCHIVE"          # or ~/.local/share/tripsy-exim
  mv "$ROOT" "$ROOT.moving"
  mkdir -p "$ROOT/staged"
  mv "$ROOT.moving" "$ROOT/staged/staged"
  ```
- `--archive` takes an archive name rather than a directory. Passing a
  path to it is an error; `--archive-root` is the flag that takes one.

### Fixed

- [models(7)](docs/models.md) said the API documents no value set for
  `activity_type`. It documents 47, and a person can add their own.
