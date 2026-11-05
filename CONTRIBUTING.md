# Contributing

## Development

The Action is one standard-library Python script plus a composite `action.yml`.
There is no build step and nothing to install.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
python3 scripts/zenodo_sync.py --config examples/full-metadata.json --mode check
```

The suite is offline. `FakeClient` replaces HTTP and fails the test if a
`check` run performs a write. Live-vocabulary behaviour is validated at runtime
and covered by tests that stub the client, so CI needs no token.

The live `check` exits `1` with `missing` for the example repository, which is
expected. To run the engine against your own declaration:

```bash
ZENODO_TOKEN=... python3 scripts/zenodo_sync.py --config path/to/zenodo-sync.json --mode check
```

The token can also be passed with `--token` or the `ZENODO_KEY` environment
variable.

Changes to a metadata shape should come with:

- the config normaliser that accepts the shape,
- the matching branch in `schema/zenodo-sync.schema.json`,
- a test in `tests/test_zenodo_sync.py`,
- a line in `examples/full-metadata.json` when the key was not covered.

## API quirks

Verified against `zenodo.org` and the developer docs:

- Send `Accept: application/vnd.inveniordm.v1+json`; the default representation
  is the legacy shape. Custom fields are top-level `custom_fields`.
- Concepts are not records. `GET /api/records/{concept}` redirects to the latest
  version; versions are listed from a version id.
- Anonymous pagination caps at 25 items; authenticated requests cap at 100.
- Search hides non-latest versions unless `allversions=true`.
- `PUT /api/records/{id}/draft` replaces the whole draft, so the engine always
  reads the draft and merges before writing. Server-derived fields are stripped
  from the payload because Zenodo rejects them on write.
- A `2xx` response can still carry field errors; any `errors` entry other than
  `files.enabled` aborts the run rather than being reported as success.

## Releasing

Version lives in `VERSION` in `scripts/zenodo_sync.py` and in `CHANGELOG.md`.
Use [Semantic Versioning](https://semver.org/).

1. Update `VERSION`, add a `CHANGELOG.md` entry, and commit.
2. Tag the release and move the major tag:

   ```bash
   git tag -a v1.0.0 -m v1.0.0
   git tag -fa v1 -m v1
   git push origin v1.0.0
   git push origin v1 --force
   ```

3. Create a GitHub release from the version tag and tick **Publish this Action to
   the GitHub Marketplace**.

Consumers should pin `@v1` for fixes or `@v1.x.y` for immutability.
