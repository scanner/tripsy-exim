# Changelog

All notable changes to this project will be documented in this file.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/scanner/tripsy-exim/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/scanner/tripsy-exim/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/scanner/tripsy-exim/releases/tag/v1.0.0
