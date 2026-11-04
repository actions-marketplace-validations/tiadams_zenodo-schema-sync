# Security policy

## Supported versions

The latest release line (`v1`) receives fixes. Older tags do not.

## Reporting a vulnerability

Report privately through GitHub Security Advisories on
[tiadams/zenodo-schema-sync](https://github.com/tiadams/zenodo-schema-sync/security/advisories/new)
Do not open a public issue for a vulnerability.

## Tokens

A Zenodo personal access token grants write access to every deposit the account
owns. Treat it as a production credential:

- Store it as a repository or environment secret, never in the declaration file.
- Use a dedicated account that owns only the records the workflow manages.
- The Action masks the token with `::add-mask::`, never prints it, and scrubs it
  from error messages. It never reads the token in `check` mode unless one is
  supplied.
- Do not run it from `pull_request_target` or `workflow_run` workflows, which can
  expose secrets to untrusted code.
- Revoke the token if a workflow log or artifact might have exposed it. Zenodo
  tokens are managed at <https://zenodo.org/account/settings/applications/>.
