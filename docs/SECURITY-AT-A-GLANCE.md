# Security at a glance

One page. Full policy: [`SECURITY.md`](../SECURITY.md).

## What blumkin is

A local CLI that acts **as the signed-in user** against their own Microsoft 365
or Google Workspace, over delegated OAuth. No server, no bot, no app-only
permissions, no multi-tenant anything.

## What it touches, and where that lives

| Data | Location | In git? |
|------|----------|---------|
| OAuth client id (public client) | `~/.config/blumkin/config.toml` (mode `0600`) | never |
| Token cache + auth record / Google token | `~/.config/blumkin/profiles/<name>/` | never |
| Google desktop-client JSON (holds `client_secret`) | operator-chosen path, mode `0600` | never |
| The user's mail / calendar / chat content | fetched on demand, printed to stdout, not persisted | n/a |

[`.gitignore`](../.gitignore) and the `gitleaks` CI gate
([`.github/ci/secret-scan`](../.github/ci/secret-scan)) keep all of the above out
of the repo. blumkin never writes another person's data anywhere.

## Auth model

- **Delegated only.** Interactive browser sign-in (public client + `localhost`
  redirect). No client secret for Microsoft flows; the Google secret stays in
  the desktop-client JSON, never in toml or env.
- The token cache and auth record are written under `~/.config/blumkin/` as
  plaintext, mode `0600`, with **no cryptographic or host binding** - a copy
  taken with the client id / tenant will refresh on another machine. Protect
  the directory; revoke tenant-side (or remove the app grant) if it leaks.
- Silent refresh renews access tokens without a browser; deleting the cache
  forces a fresh sign-in.

## Blast radius

- A skill only ever affects the operator's own tenant, with their own consent.
- Actions that reach other people (mail send, calendar invite, chat) require an
  explicit `--yes` and a verb that says so - read-looking commands never send.
- Worst case for a stolen `~/.config/blumkin/`: the attacker can act as the
  operator until the refresh token is revoked (tenant-side) or the app grant is
  removed.

## How releases are trusted

1. Every PR: agent review + code-owner review, plus the required checks in
   [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) - `ruff` / `pyright`
   ([`.github/ci/python-static`](../.github/ci/python-static)), `pytest`
   ([`pytest-hermetic`](../.github/ci/pytest-hermetic)), `shellcheck`, and
   [`test_packaging`](../test_packaging) - all green.
2. Release cut by Release Please from Conventional Commits
   ([`.github/workflows/release-please.yml`](../.github/workflows/release-please.yml),
   [`release-please-config.json`](../release-please-config.json)) - no
   hand-edited versions.
3. Published to PyPI by **OIDC trusted publishing** (no stored token). The wheel
   carries the release commit via
   [`scripts/embed_build_metadata`](../scripts/embed_build_metadata) /
   [`src/blumkin/version.py`](../src/blumkin/version.py) - `blumkin --version`
   shows it.
4. [`scripts/verify-pypi-release`](../scripts/verify-pypi-release) installs the
   published artifact in isolation and checks the version and commit before the
   job is green.

Compliance of the branch protection, workflows, and cursor rules that back all
of this is enforced by `github-repo-lint` from
[`the-hcma/repository-helpers`](https://github.com/the-hcma/repository-helpers) -
see [`SECURITY.md` › Governance tooling](../SECURITY.md#governance-tooling-repository-helpers).

## Vulnerabilities

- Dependencies: `pip-audit` daily
  ([`.github/workflows/cve-check.yml`](../.github/workflows/cve-check.yml)) +
  Dependabot ([`.github/dependabot.yml`](../.github/dependabot.yml), 10-day
  cooldown).
- Report a vulnerability privately:
  <https://github.com/the-hcma/blumkin/security/advisories/new>. Targets and
  scope are in [`SECURITY.md`](../SECURITY.md).
