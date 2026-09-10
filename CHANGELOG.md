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
