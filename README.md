# Zenodo schema sync

[![CI](https://github.com/tiadams/zenodo-schema-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/tiadams/zenodo-schema-sync/actions/workflows/ci.yml)
[![GitHub Marketplace](https://img.shields.io/badge/marketplace-Zenodo%20schema%20sync-blue?logo=github)](https://github.com/marketplace/actions/zenodo-schema-sync)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A GitHub Action that declares a repository's [Zenodo](https://zenodo.org) record
metadata in **Zenodo's current schema** — including the CodeMeta "Software"
fields — and applies it to the record after every release.

## The problem

Zenodo's GitHub integration only understands Zenodo's **legacy** metadata schema.
It still archives releases, but anything a modern record can hold — CodeMeta
`custom_fields`, subjects, dates, locations, funding, references — is discarded.

The loss happens in one line,
[`site/zenodo_rdm/github/release.py:41`](https://github.com/zenodo/zenodo-rdm/blob/master/site/zenodo_rdm/github/release.py#L41):

```python
rdm_data = LegacySchema().load(legacy_data)
return rdm_data["metadata"]        # "custom_fields" is dropped here
```

Declaring the fields in `.zenodo.json` does not help: the legacy schema excludes
unknown keys, and CITATION.cff is ignored whenever `.zenodo.json` is present. So
every GitHub-created release silently wipes hand-set CodeMeta fields, and no
repository file can prevent it. If you have ever thought *"Zenodo wiped my
CodeMeta fields again"*, this is why. Upstream has
[no plans to fix it](#why-this-exists-and-when-it-goes-away).

## What it does

You declare the fields you own in `.github/zenodo-sync.json`, in the **current**
schema. The Action validates the declaration against the live Zenodo instance,
then reads the existing draft, merges only the declared keys, and — in `sync`
mode — publishes. Everything you did not declare is preserved byte-for-byte.

`check` is read-only and reports drift with a job summary and exit code.
`sync` writes, and is idempotent: a matching record causes no draft and no new
revision, so a repeated run performs no write.

```yaml
- uses: tiadams/zenodo-schema-sync@v1
  with:
    mode: sync
    token: ${{ secrets.ZENODO_TOKEN }}
```

The same engine runs locally, with no install step:

```bash
python3 scripts/zenodo_sync.py --mode check
python3 scripts/zenodo_sync.py --mode sync
```

## Quick start

**1. Declare your metadata** in `.github/zenodo-sync.json`:

```json
{
  "repository": "https://github.com/example/project",
  "version": "v1.2.3",
  "metadata": {
    "title": "Project",
    "resource_type": { "id": "software" },
    "publication_date": "2026-09-28",
    "rights": [{ "id": "cc-by-4.0" }],
    "creators": [
      {
        "person_or_org": {
          "type": "personal",
          "given_name": "Jane",
          "family_name": "Doe"
        }
      }
    ]
  },
  "custom_fields": {
    "code:codeRepository": "https://github.com/example/project",
    "code:programmingLanguage": ["python"],
    "code:developmentStatus": "active"
  }
}
```

**2. Add a workflow** — for example `.github/workflows/zenodo.yml`:

```yaml
name: Zenodo metadata
on:
  release:
    types: [published]
  workflow_dispatch:
    inputs:
      tag:
        description: Release tag to sync, for example v1.2.3
        required: true

permissions:
  contents: read

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: tiadams/zenodo-schema-sync@v1
        with:
          mode: sync
          version: ${{ github.event.release.tag_name || inputs.tag }}
          token: ${{ secrets.ZENODO_TOKEN }}
```

`check` is the default mode and is read-only, so it is safe to run on pull
requests or a schedule. It writes a Job Summary with the exact per-field diff
and exits non-zero when the record drifts or the release was never archived.

A complete declaration is in
[`examples/full-metadata.json`](examples/full-metadata.json); the machine-readable
contract is [`schema/zenodo-sync.schema.json`](schema/zenodo-sync.schema.json).

## Declaring metadata

Only keys present in the config are owned by the Action. Everything else —
access, files, permissions, and any metadata key you do not declare — is merged
from the existing draft and kept.

| Key | Notes |
| --- | --- |
| `repository` | Repository URL anchor used to resolve the record. `.git` is stripped. |
| `record` / `concept` | Explicit record or concept (parent) id. Pick one; skips search. |
| `version` | Release version, e.g. `v1.2.3`. Matched against `metadata.version`. |
| `metadata` | Current-schema metadata. Strings are accepted for vocabulary ids: `"resource_type": "software"`. |
| `custom_fields` | CodeMeta plus the `legacy:`, `journal:`, `meeting:`, `imprint:`, `thesis:`, `obo:` and domain namespaces. `custom` is accepted as an alias. |
| `access` | Optional `record`/`files` visibility (`public`/`restricted`) and embargo. |
| `files` | Optional `{ "enabled": true }`. Default enables files and uploads the tag archive on creation. |

### Metadata shapes

Every key accepts the shape Zenodo itself returns. Shorthands are expanded
before writing:

- **People.** `{"type": "personal", "name": "Doe, Jane"}` splits into
  `given_name`/`family_name`; organizations derive `name` from their parts.
  Affiliations take `"CERN"`, `{"id": "05a28rw58"}`, or `{"name": "..."}`;
  ids are Zenodo's internal affiliation ids, not ROR URLs.
- **Vocabularies.** `"software"`, `{"id": "software"}`, or the full API object
  are equivalent; only `id` is sent.
- **Rights.** Either a license id (`"cc-by-4.0"`) or free text with `title`,
  `description` and `link`; the two cannot be mixed.
- **Funding.** `funder` is a Zenodo funder id (e.g. `"04jsz6e67"`) or a
  free-text `name`; a DOI/ROR URL is rejected because Zenodo resolves only its
  own ids. `award` is a Zenodo award id or an inline award with `number`,
  `title` and DOI/URL `identifiers`.
- **References, subjects, formats, sizes.** Plain strings or current-schema
  objects.
- **Dates.** EDTF strings (`2024-01-01`, `2020/2021`).
- **Locations.** GeoJSON `Point` and `Polygon` features with
  `geonames`/`wikidata` identifiers. `MultiPoint` and `MultiPolygon` are
  rejected: Zenodo nulls `MultiPoint` coordinates in its UI serializer, which
  makes the record page return HTTP 500, and `MultiPolygon` is outside the
  InvenioRDM schema.
- **Nested custom fields.** `journal:journal`, `meeting:meeting`,
  `imprint:imprint` and `thesis:thesis` are single objects with fixed keys.
  ISSNs and ISBNs are checksum-validated; thesis dates must be EDTF.
- **Domain custom fields.** `dwc:`, `gbif-dwc:`, `gbif:`, `mixs:`, `ac:`,
  `openbiodiv:` and `dc:` accept a scalar or list of scalars and are always
  written as lists. Only field names Zenodo defines are accepted.
  `obo:RO_0002453` takes `subject`/`object` term lists; other `obo:` fields
  accept scalars or lists.

Vocabulary ids are validated against the live instance before any write. Unknown
values fail in `check` with the offending JSON path and a suggestion:

```
config.json.metadata.resource_type.id: unknown resource_type id 'softwear' (did you mean: software, other)
```

Validation covers resource types, licenses, languages, relation types, creator
and contributor roles, date/description/title types, CodeMeta programming
languages and development status, funders, awards, communities and affiliations.
Zenodo requires `metadata.publisher` when registering a DOI, so a config that
creates a new deposit must set it.

Resolution order: explicit `record` → `concept` → search by
`code:codeRepository` (and `metadata.related_identifiers`) plus `metadata.version`.
Search uses `allversions=true`, so historical versions are found even though
Zenodo's default search hides them.

## Modes and safety

| Mode | Writes | Exit codes |
| --- | --- | --- |
| `check` (default) | never | `0` in sync, `1` drift/missing/duplicates, `2` invalid config, `3` API error, `4` publish timeout |
| `sync` | create draft → merge → publish | `0` success (including already in sync), `1` duplicates, `2` invalid config, `3` API error, `4` publish timeout |

- `sync --dry-run` prints the plan and writes nothing.
- Publishing is asynchronous. New deposits and versions are polled until
  `is_published` is true or `publish_timeout` (default `300` seconds) elapses.
- A missing release is recovered by creating a new version of the latest record
  (or a new deposit if there is none) and publishing it. For GitHub repositories
  the release tag archive is downloaded, uploaded, and committed before
  publishing; if the tag archive is unavailable the run fails before any publish.
  For non-GitHub repositories the new version links the previous files instead.
  Set `"files": {"enabled": false}` to skip files, in which case a new version
  keeps the previous files.
- Duplicate deposits for one version are reported with ids and URLs and exit
  non-zero. The Action never deletes published records — only Zenodo support can.

## Inputs and outputs

| Input | Default | Description |
| --- | --- | --- |
| `mode` | `check` | `check` or `sync`. |
| `token` | — | Zenodo personal access token. Required for `sync`; optional for `check`. |
| `config` | `.github/zenodo-sync.json` | Declaration path. |
| `record` / `concept` | — | Explicit ids; override the config file. |
| `repository` / `version` | — | Override the config file. |
| `base-url` | `https://zenodo.org` | Use `https://sandbox.zenodo.org` for testing. |
| `dry-run` | `false` | Print the plan without writing. |
| `publish-timeout` | `300` | Seconds to wait for publication. |

Outputs: `status` (`in-sync`, `drift`, `missing`, `updated`, `created`,
`created-version`, `duplicates`), `record-id`, `record-url`.

The Action runs on any Linux runner with `python3` (all GitHub-hosted Ubuntu
images qualify). It uses only the Python standard library, so there is no install
step.

## Security

A Zenodo personal access token grants write access to **every deposit the account
owns**. Treat it accordingly.

- Use a dedicated service account that owns only the records in question, or
  accept the risk explicitly.
- Do **not** use this Action with `pull_request_target` or `workflow_run`. Both
  can expose secrets to untrusted code. `workflow_dispatch` is strictly safer
  than `release: published`; the release recipe above is fine because forks do
  not fire release events and get no secrets.
- Do not run the job with `set -x`. The Action never echoes the token and scrubs
  it from error messages.
- `check` works anonymously against public records, so it is safe to run on a
  schedule without a token.

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## API caveats baked into this Action

Verified against `zenodo.org` and the developer docs:

- Send `Accept: application/vnd.inveniordm.v1+json`; the default representation
  is the legacy shape. Custom fields are top-level `custom_fields`.
- Concepts are not records. `GET /api/records/{concept}` redirects to the latest
  version; versions are listed from a version id.
- Anonymous pagination caps at 25 items; authenticated requests cap at 100.
- Search hides non-latest versions unless `allversions=true`.
- `PUT /api/records/{id}/draft` replaces the whole draft, so the Action always
  reads the draft and merges before writing. Server-derived fields are stripped
  from the payload because Zenodo rejects them on write.
- A `2xx` response can still carry field errors; any `errors` entry other than
  `files.enabled` aborts the run rather than being reported as success.

## Development

Standard library only, no install step:

```bash
python3 -m unittest discover -s tests -v
python3 scripts/zenodo_sync.py --config .github/zenodo-sync.json --mode check
```

The test suite is fully offline: `FakeClient` substitutes HTTP and fails if a
`check` run performs any write.

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to release a new version.

## Why this exists, and when it goes away

The integration is in maintenance-only mode at the schema layer. The requests are
old and unowned:

- [`inveniosoftware/invenio-vcs#9`](https://github.com/inveniosoftware/invenio-vcs/issues/9)
  — the actual fix (repo-level extra metadata merged into release metadata); open
  since 2026-02-12, **no milestone, no assignee, 0 comments**.
- [`zenodo/zenodo-rdm#1205`](https://github.com/zenodo/zenodo-rdm/issues/1205) —
  open since 2018; maintainer, 2025-07-28: *"We're not currently planning to work
  on this"*.
- [`zenodo/zenodo#1504`](https://github.com/zenodo/zenodo/issues/1504) — GitHub
  CodeMeta support, open since 2018-06-19, last activity 2023-02-01.
- Zenodo's [public roadmap](https://about.zenodo.org/roadmap/) (updated
  2026-08-20) lists nothing on GitHub metadata or custom fields.

This Action is a workaround with a shelf life. It should be deprecated the moment
`invenio-vcs#9` ships, and this README should keep saying so.
