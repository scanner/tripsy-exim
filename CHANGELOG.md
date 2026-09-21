# Changelog

All notable changes to this project will be documented in this file.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing has been released yet; what the project can currently do is
described in [README.md](README.md).

### Added

- `backfill report` says what the staged trips still need a person for,
  with the places listed commonest first.
- `backfill export` writes an editable work-list of what is open, and
  `backfill apply` reads it back as corrections.
- `backfill infer` places an endpoint from an airport code the archive
  already positions elsewhere, refusing a code that names two places
  -- two airports serving one city, say, or a code like `TYO` that
  names a metropolitan area rather than an airport.
