# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## 1.0.0 - 2026-11-04

First public release.

- Composite GitHub Action that declares Zenodo record metadata in the current
  schema and applies it to the record after a release. Standard library only,
  no install step.
- `check` mode: read-only per-field drift report, Job Summary, and non-zero exit
  on drift, missing deposits, or duplicates.
- `sync` mode: merge the declaration into the record draft, publish, and poll to
  a bounded timeout. Idempotent; a matching record causes no write.
- Current-schema `metadata` and CodeMeta `custom_fields`, with `custom` accepted
  as an alias. Undeclared fields are always preserved.
- Shorthand expansion for people, vocabularies, rights, funding, references,
  dates, locations, and nested custom fields.
- Validation against live vocabularies before any write, with JSON paths and
  suggestions on failure.
- Record resolution by explicit id, concept id, or repository URL plus version,
  including non-latest versions. Duplicates are reported with ids and URLs and
  never deleted.
- Missing release recovery: create a new version from the latest record, or a
  new deposit when none exists. For GitHub repositories the release tag archive
  is downloaded, uploaded, and committed before publishing.
- Reject `MultiPoint` locations. Zenodo stores them but its UI serializer
  returns `coordinates: null`, which makes the record page return HTTP 500.
  `Point` and `Polygon` are unaffected.
- Strip server-derived fields from draft metadata before writing: affiliation
  `identifiers` and right `icon`/`props` are read-only, and duplicate
  affiliations or a licence id combined with its title are rejected by Zenodo.
- Offline test suite (`unittest` with a `FakeClient`), schema validation, and CI
  across Python 3.10–3.13.
