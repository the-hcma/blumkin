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
- The release pipeline
  ([`.github/workflows/release-please.yml`](.github/workflows/release-please.yml),
  trusted publishing, and the build scripts
  [`scripts/embed_build_metadata`](scripts/embed_build_metadata) /
  [`scripts/verify-pypi-release`](scripts/verify-pypi-release)).
- Handling of the local token cache and config under `~/.config/blumkin/`
  ([`src/blumkin/auth.py`](src/blumkin/auth.py),
  [`src/blumkin/config.py`](src/blumkin/config.py),
  [`src/blumkin/providers/google_auth.py`](src/blumkin/providers/google_auth.py)).

## What is out of scope

- Microsoft Graph / Google Workspace themselves, and the tenant's own consent
  and conditional-access policies.
- The security of a machine where an operator has run `blumkin auth login` -
  the token cache is only as protected as `~/.config/blumkin/` on that host.
- Social-engineering an operator into running a notifying skill (`--yes` is the
  guard; see [`.cursor/rules/no-third-party-side-effects.mdc`](.cursor/rules/no-third-party-side-effects.mdc)).

## Controls in place

Every check links to the code that runs it. The **required status checks** on
`main` are the hard merge gate; the rest are advisory and caught at code-owner
review.

| Control | Where | Gate |
|---------|-------|------|
| Code review | agent review (`mergestorm-vortex`) on every PR head with reply-before-resolve ([`.cursor/rules/pr-ship-and-review.mdc`](.cursor/rules/pr-ship-and-review.mdc)); code owner (`@thehcma`) requested on every PR via [`.github/CODEOWNERS`](.github/CODEOWNERS) - `required_approving_review_count` is 0 for a solo maintainer, see [`docs/DECISIONS.md`](docs/DECISIONS.md) D2 | review threads addressed before merge |
| Static analysis | `ruff` + `pyright` + [`.github/ci/assert-uv-lock-version`](.github/ci/assert-uv-lock-version), run by [`.github/ci/python-static`](.github/ci/python-static) (`[tool.ruff]` / `[tool.pyright]` in [`pyproject.toml`](pyproject.toml)); job [`ci.yml` › `python-static`](.github/workflows/ci.yml) | **required check `Python lint & format checks` - blocks merge** |
| Tests | `pytest -m 'not live'` via [`.github/ci/pytest-hermetic`](.github/ci/pytest-hermetic); job [`ci.yml` › `test`](.github/workflows/ci.yml) | **required check `Pytest (hermetic)` - blocks merge** |
| Installed-artifact check | [`test_packaging`](test_packaging) - build the wheel, install it outside the repo, run the console script; job [`ci.yml` › `packaging`](.github/workflows/ci.yml) | **required check `Packaging smoke` - blocks merge** |
| Shell lint | `shellcheck -S info` via [`.github/ci/shellcheck`](.github/ci/shellcheck); job [`ci.yml` › `shellcheck`](.github/workflows/ci.yml) | **required check `Shellcheck` - blocks merge** |
| Secret scanning (GitHub-native) | GitHub secret scanning + **push protection** on `the-hcma/blumkin` (Settings › Code security) - blocks a recognised secret from being pushed at all | **blocks the push** |
| Secret scanning (CI) | `gitleaks` via [`.github/ci/secret-scan`](.github/ci/secret-scan) (org-canonical, synced by `github-repo-lint`), every PR + push; job [`ci.yml` › `secret-scan`](.github/workflows/ci.yml) | runs every PR; advisory (not a required check) - relies on code-owner review + push protection |
| Dependency CVE scan | `pip-audit` via [`.github/workflows/cve-check.yml`](.github/workflows/cve-check.yml), daily + on demand | advisory - files a `security/cve` issue |
| Dependency alerts | GitHub **Dependabot alerts + security updates** enabled (advisory-database CVEs, separate from `pip-audit`) | alerts + auto-fix PRs |
| Dependency updates | Dependabot ([`.github/dependabot.yml`](.github/dependabot.yml), 10-day cooldown) + auto-merge ([`.github/workflows/dependabot-auto-merge.yml`](.github/workflows/dependabot-auto-merge.yml)) | opens PRs; auto-merges once required checks pass |
| Vulnerability intake | **Private vulnerability reporting** enabled - [report an advisory](https://github.com/the-hcma/blumkin/security/advisories/new) | private channel, no public disclosure |
| Actions permissions | `allowed_actions: selected` (GitHub-owned + an explicit allowlist: `astral-sh/setup-uv`, `googleapis/release-please-action`, `nick-fields/retry`, `pypa/gh-action-pypi-publish`); default workflow token is **read-only**; the token **cannot approve PRs**; **every `uses:` in every workflow is SHA-pinned** with a `# vX.Y.Z` comment (Dependabot keeps them current) | limits third-party code in CI / the OIDC path |
| Publish gate | The [`pypi` environment](https://github.com/the-hcma/blumkin/settings/environments) requires **@thehcma to approve** each deployment and only allows deploys from `main` / `blumkin-v*` tags | pauses every PyPI publish for a human |
| Supply chain | PyPI **trusted publishing** (OIDC, no long-lived token) in [`release-please.yml` › `publish-pypi`](.github/workflows/release-please.yml); [`scripts/embed_build_metadata`](scripts/embed_build_metadata) records the release commit in the wheel ([`src/blumkin/version.py`](src/blumkin/version.py)); [`scripts/verify-pypi-release`](scripts/verify-pypi-release) re-checks the published artifact | gates the release job |
| Org repo-practice compliance | `github-repo-lint` (branch-protection shape, required workflows, CODEOWNERS, Dependabot cooldown, cursor-rule sync) - see [Governance tooling](#governance-tooling-repository-helpers) | `repo-practices-lint` in `pre-pr-checks`; local gate |

## Governance tooling (repository-helpers)

The compliance and workflow tooling above is **not vendored into this repo**. It
comes from
[`the-hcma/repository-helpers`](https://github.com/the-hcma/repository-helpers)
and is versioned there, re-synced into this repo per `AGENTS.md` ("Repository
practices / lint" and "Formatting & linting").

| Tool | Purpose | Gate |
|------|---------|------|
| [`github-repo-lint`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/github-repo-lint) | Enforces the org repo-practice contract: branch-protection shape, required workflows, CODEOWNERS, Dependabot cooldown, agent-bootstrap shims, and that [`.cursor/rules/*.mdc`](.cursor/rules) match the canonical templates in [`repo-practices-cursor/`](https://github.com/the-hcma/repository-helpers/tree/main/scripts/lib/repo-practices-cursor) | hard - `repo-practices-lint` step of `pre-pr-checks` |
| [`.cursor/rules/*.mdc`](.cursor/rules) | Coding + workflow rules read at the start of every session (`no-secret-exposure`, `no-third-party-side-effects`, `remote-timeouts-retries`, `git-commit-identity`, `pr-ship-and-review`, `stacking-tool`, `pre-pr-checks`, `main-worktree-off-limits`, `lexicographic-code-organization`, `local-live-graph-tests`, `read-agents-and-rules`, `repo-practices-after-config-change`) - consumer copies of the [canonical templates](https://github.com/the-hcma/repository-helpers/tree/main/scripts/lib/repo-practices-cursor) | reviewed at code-owner review; sync enforced by `github-repo-lint` |
| [`.github/ci/secret-scan`](.github/ci/secret-scan) | `gitleaks` gate - **byte-identical** to [`scripts/lib/ci-secret-scan`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/lib/ci-secret-scan); do not hand-edit, sync via `github-repo-lint --apply-fix` | advisory check + `github-repo-lint` |
| [`pre-pr-checks`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/dev/pre-pr-checks) | Local pre-submit gate: runs `python-static`, `pytest-hermetic`, `bash -n`, `shellcheck`, `repo-practices-lint`, `secret-scan`, verified-commits | run before every submit |
| [`start-development`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/dev/start-development) | Worktree-per-stack setup | session start |
| [`wait-for-agent-review`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/wait-for-agent-review) | Drives the agent-review loop (reply-before-resolve, CI wait, operator email) | mandatory per PR |
| [`ship-and-review`](https://github.com/the-hcma/repository-helpers/blob/main/.cursor/skills/ship-and-review/SKILL.md) / [`gh-stack`](https://github.com/the-hcma/repository-helpers/blob/main/.cursor/skills/gh-stack/SKILL.md) skills | The canonical submit + stacking playbooks | followed on every PR |

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full toolchain and the review
model, and [`docs/DECISIONS.md`](docs/DECISIONS.md) for why the repo is public
and in a personal org.
