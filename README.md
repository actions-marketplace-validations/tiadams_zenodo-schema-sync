# Zenodo schema sync

[![CI](https://github.com/tiadams/zenodo-schema-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/tiadams/zenodo-schema-sync/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A GitHub Action that keeps a repository's [Zenodo](https://zenodo.org) record
metadata complete after every release, including most of what the Zenodo deposit
form supports but the GitHub integration silently drops.

## The problem

Zenodo's GitHub integration archives releases, but it describes them with Zenodo's
**legacy** schema only, and it keeps just part of what that schema produces.
Zenodo's own legacy deserializer does build the modern `custom_fields`, `access`,
and `pids` sections. Then
[`site/zenodo_rdm/github/release.py:41`](https://github.com/zenodo/zenodo-rdm/blob/master/site/zenodo_rdm/github/release.py#L41)
throws them away:

```python
rdm_data = LegacySchema().load(legacy_data)
return rdm_data["metadata"]        # custom_fields, access, pids never reach the record
```

Keys the legacy schema does not know disappear one step earlier, because its
`MetadataSchema` silently excludes them. So a `.zenodo.json` cannot carry most of
what the deposit form supports, and `CITATION.cff` is ignored whenever
`.zenodo.json` is present.

What a hand-described record loses the next time a GitHub release is archived:

| Field | After a GitHub release |
| --- | --- |
| Custom fields: CodeMeta `code:*`, `journal:`, `meeting:`, `imprint:`, `thesis:`, domain vocabularies | dropped with `custom_fields` |
| Communities from `.zenodo.json` | ignored; existing parent membership is kept |
| Access level and embargo | reset to public record and public files |
| Reserved DOI from `.zenodo.json` | dropped with `pids` |
| Subjects from EuroSciVoc, MeSH, GEMET | dropped; only free-text `keywords` are mapped |
| Additional titles: subtitle, translated, alternative | dropped |
| Publisher | replaced by the instance default |
| Sizes, formats, copyright | dropped |
| Locations | only `place`, `description` and a `lat`/`lon` point survive; polygons and GeoNames/Wikidata identifiers are lost |
| Funding | funder and award ids survive; inline award titles and identifiers are lost |

The fields the legacy mapper does understand (title, description, version,
resource type, publication date, creators, contributors, license, language,
dates, references, related identifiers, and free-text keywords) are re-derived
rather than preserved, so their shapes are flattened too.

Upstream has [no plans to fix it](#why-this-exists-and-when-it-goes-away).

## What it does

You declare the fields you own in `.github/zenodo-sync.json`, in the **current**
schema. The Action validates the declaration against the live Zenodo instance,
then reads the existing draft and merges only the declared keys. In `sync` mode it
publishes. Keys you do not declare are left as the draft has them.

This restores the fields in the table above, with two exceptions: it does not
reserve a DOI (an existing reservation is kept) and cannot create a community
membership (join once in the record UI). It does not read `.zenodo.json` or
`CITATION.cff`, so the declaration is the only source of truth for the keys it
owns.

`check` reports drift with a job summary and an exit code. `sync` is idempotent:
a record that already matches causes no draft and no new revision.

```yaml
- uses: tiadams/zenodo-schema-sync@v1
  with:
    mode: sync
    token: ${{ secrets.ZENODO_TOKEN }}
```

## Quick start

**1. Create a token.** Make a Zenodo [personal access
token](https://zenodo.org/account/settings/applications/tokens/new/) with the
`deposit:write` and `deposit:actions` scopes. Add it to the repository as the
`ZENODO_TOKEN` secret (`Settings → Secrets and variables → Actions`). `check`
works anonymously against public records, so a token is only needed for `sync`.

**2. Declare your metadata** in `.github/zenodo-sync.json`:

```json
{
  "repository": "https://github.com/example/project",
  "version": "v1.2.3",
  "metadata": {
    "title": "Project",
    "resource_type": { "id": "software" },
    "publication_date": "2026-09-28",
    "publisher": "Example Institute",
    "rights": [{ "id": "cc-by-4.0" }],
    "creators": [
      {
        "person_or_org": {
          "type": "personal",
          "given_name": "Jane",
          "family_name": "Doe"
        }
      }
    ],
    "subjects": ["Computer science", "Reproducibility"],
    "additional_titles": [
      { "title": "A reproducibility toolkit", "type": "subtitle" }
    ],
    "sizes": ["12 MB"],
    "formats": ["application/zip"]
  },
  "custom_fields": {
    "code:codeRepository": "https://github.com/example/project",
    "code:programmingLanguage": ["python"],
    "code:developmentStatus": "active"
  }
}
```

**3. Add a workflow** — for example `.github/workflows/zenodo.yml`:

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

This runs on the release event, so it assumes Zenodo's own GitHub integration is
**off** for the repository. If it is on, see
[Using it with Zenodo's GitHub integration](#using-it-with-zenodos-github-integration)
for a race-free trigger.

`check` is the default mode, so a job without `mode` is safe on pull requests and
schedules: it writes a per-field diff to the job summary and exits non-zero when
the record drifts or the release was never archived.

A complete declaration is in
[`examples/full-metadata.json`](examples/full-metadata.json); the machine-readable
contract is [`schema/zenodo-sync.schema.json`](schema/zenodo-sync.schema.json).

## Using it with Zenodo's GitHub integration

Zenodo archives the release; the Action fills in the metadata afterwards. With a
`version` set, it finds the record by `code:codeRepository` or by the GitHub
`related_identifiers` entry Zenodo writes, so it works whether the integration
created the record or the Action did. Without a version it falls back to
`code:codeRepository` alone.

Do not trigger the Action on the same `release: published` event as the
integration. Both race to create the deposit, and if the Action wins Zenodo
creates a second record moments later. Trigger the Action after the release
instead, for example with `workflow_dispatch` and the tag as an input, or disable
the integration and let the Action create the deposit from the config.

If two records do get created, the Action detects the pair on its next run and
fails with both ids rather than guessing.

## Declaring metadata

Only keys present in the config are owned by the Action. Access, files,
permissions and any metadata key you leave out are merged from the existing draft
and kept.

| Key | Notes |
| --- | --- |
| `repository` | Repository URL anchor used to resolve the record. `.git` is stripped. Not inferred from the workflow, so set it here or pass the `repository` input. |
| `record` / `concept` | Explicit record or concept (parent) id. Pick one; skips search. |
| `version` | Release version, e.g. `v1.2.3`. Matched against `metadata.version`. Omit to target the latest version. |
| `metadata` | Current-schema metadata. Strings are accepted for vocabulary ids: `"resource_type": "software"`. |
| `custom_fields` | Current-schema custom fields: CodeMeta `code:*`, `journal:`, `meeting:`, `imprint:`, `thesis:`, `legacy:communities`, `obo:` and the domain vocabularies. `custom` is accepted as an alias. |
| `access` | Optional `record`/`files` visibility (`public`/`restricted`) and embargo. |
| `files` | Optional `{ "enabled": true }`. When omitted, an existing record keeps its current file setting, and a newly created one uploads the tag archive if the repository is on GitHub. |

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
  objects. Controlled-vocabulary subjects keep their `scheme` and `identifier`.
- **Communities.** `legacy:communities` validates and stores community ids in
  `custom_fields`, but Zenodo only turns that field into membership through its
  legacy API. Join the community in the record's UI; the parent membership is
  kept across versions.
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
Creating a new deposit (rather than updating an existing one) also requires
`metadata.title`, `creators`, `resource_type`, `publication_date` and
`publisher`; Zenodo requires a publisher when registering a DOI.

Resolution order: explicit `record` → `concept` → search by
`code:codeRepository` (and `metadata.related_identifiers`) plus `metadata.version`.
Search uses `allversions=true`, so historical versions are found even though
Zenodo's default search hides them.

If `repository` is set, the resolved record must link to it through
`code:codeRepository` or a GitHub `related_identifiers` entry, or the run refuses
to modify it. To adopt an older record that predates the link, point at it with
`record`/`concept` and drop `repository`, or add the link in the Zenodo UI first.

If a token is used, it must belong to the account that owns the record. The Action
cannot edit another user's deposit.

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
  The tag archive is fetched anonymously, so a private repository must set
  `"files": {"enabled": false}`, which also makes a new version keep the previous
  files.
- Duplicate deposits for one version are reported with ids and URLs and exit
  non-zero. Published records are never deleted; only Zenodo support can do that.

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

The Action runs on any Linux runner with `python3`, which includes all
GitHub-hosted Ubuntu images.

## Security

A Zenodo personal access token grants write access to **every deposit the account
owns**. Treat it accordingly.

- Use a dedicated service account that owns only the records in question, or
  accept the risk explicitly.
- Do **not** use this Action with `pull_request_target` or `workflow_run`. Both
  can expose secrets to untrusted code. `workflow_dispatch` is strictly safer
  than `release: published`; the release recipe above is safe from a secrets
  standpoint because forks do not fire release events and get no secrets.
- Do not run the job with `set -x`. The Action never echoes the token and scrubs
  it from error messages.
- `check` works anonymously against public records, so it is safe to run on a
  schedule without a token.

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for running the tests locally, the API
quirks the engine works around, and the release process.

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
`invenio-vcs#9` ships.
