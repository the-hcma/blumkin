# Security policy

blumkin is a personal Microsoft 365 / Google Workspace CLI that acts as the
signed-in user over **delegated** OAuth. It holds no server, no shared secret,
and no other person's data. For the short version see
[`docs/SECURITY-AT-A-GLANCE.md`](docs/SECURITY-AT-A-GLANCE.md).

## Reporting a vulnerability

Report privately, not in a public issue or PR:

- **GitHub private vulnerability reporting** — <https://github.com/the-hcma/blumkin/security/advisories/new>
  (Security tab -> Report a vulnerability).

Include the version (`blumkin --version`), the platform, and the smallest
reproduction you have. You will get an acknowledgement within **3 business
days**.

Please do not open a normal issue, post a PR, or disclose publicly until a fix
is released or 90 days have passed, whichever comes first.

## Supported versions

blumkin ships from `main` only. Fixes go into the next release; there are no
back-port branches.

| Version | Supported |
|---------|-----------|
| Latest PyPI release | yes |
| Anything older | no - upgrade with `blumkin upgrade` |

## Response targets

| Severity | Triage | Fix released |
|----------|--------|--------------|
| Critical (token/credential exposure, RCE) | 3 business days | 7 days |
| High | 5 business days | 30 days |
| Medium / Low | 10 business days | next routine release |

A fix is a normal Conventional-Commit `fix:` PR; merging it and its release PR
publishes a patched version through the pipeline in
[`docs/RELEASING.md`](docs/RELEASING.md). If a published release must be pulled,
follow the **Rollback** section there.

## What is in scope

- The `blumkin` CLI and its packaged code.
- The release pipeline (`release-please.yml`, trusted publishing, the build
  scripts under `scripts/`).
- Handling of the local token cache and config under `~/.config/blumkin/`.

## What is out of scope

- Microsoft Graph / Google Workspace themselves, and the tenant's own consent
  and conditional-access policies.
- The security of a machine where an operator has run `blumkin auth login` -
  the token cache is only as protected as `~/.config/blumkin/` on that host.
- Social-engineering an operator into running a notifying skill (`--yes` is the
  guard; see [`.cursor/rules/no-third-party-side-effects.mdc`](.cursor/rules/no-third-party-side-effects.mdc)).

## Controls in place

| Control | Where | Gate |
|---------|-------|------|
| Code review | code-owner (`@thehcma`) approval required on every PR + `require_last_push_approval`; agent review (`mergestorm-vortex`) with reply-before-resolve | **blocks merge** |
| Static analysis | `ruff` + `pyright` via `.github/ci/python-static` (`Python lint & format checks`) | **required check - blocks merge** |
| Tests | `pytest -m 'not live'` (`Pytest (hermetic)`) | **required check - blocks merge** |
| Installed-artifact check | `test_packaging` (`Packaging smoke`) | **required check - blocks merge** |
| Shell lint | `shellcheck` (`Shellcheck`) | **required check - blocks merge** |
| Secret scanning (`gitleaks`) | `.github/ci/secret-scan`, every PR + push | runs every PR; advisory (not a required check) - relies on code-owner review |
| Dependency CVE scan (`pip-audit`) | `.github/workflows/cve-check.yml`, daily + on demand | advisory - files a `security/cve` issue |
| Dependency updates | Dependabot, `.github/dependabot.yml`, 10-day cooldown | opens PRs |
| Supply chain | PyPI **trusted publishing** (OIDC, no long-lived token); `_build_metadata.py` records the release commit in the wheel; `scripts/verify-pypi-release` re-checks the published artifact | gates the release job |

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full toolchain and the review
model, and [`docs/DECISIONS.md`](docs/DECISIONS.md) for why the repo is public
and on a personal account.
