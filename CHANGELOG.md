# Changelog

All notable changes to this project will be documented in this file.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.2] - 2026-09-24

### Added

- Support HashiCorp Vault as a secret store, with `hcvault://` URLs.
- Ask for the Tripsy username and password at a terminal when no flag,
  environment variable or store supplies them.
- `auth check`, which authenticates and says how the token was found.
- `list --uploaded`, which lists the trips in your Tripsy account, with
  or without a staged archive.

### Changed

- Report each login, and whether the token was saved, on standard error.

## [1.0.1] - 2026-09-24

### Added

- Fill a flight's airline, flight number, terminals and gates when
  staging from `.ics` calendars.

### Fixed

- Replace TripIt's tracking redirects in `.ics` event notes with the links
  they hide, so re-exporting a trip no longer changes its descriptions.

## [1.0.0] - 2026-09-21

The first release. Import from a TripIt JSON export or `.ics` files into
Tripsy, and export what Tripsy holds into dated local backups. See
[README.md](README.md) for what the project does and how to use it.

[Unreleased]: https://github.com/scanner/tripsy-exim/compare/v1.0.2...HEAD
[1.0.2]: https://github.com/scanner/tripsy-exim/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/scanner/tripsy-exim/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/scanner/tripsy-exim/releases/tag/v1.0.0
