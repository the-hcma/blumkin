# Contributing to blumkin

blumkin is a personal tool with a single maintainer / code owner (@thehcma).
Contributions are welcome as issues and pull requests; the bar is the same for
everyone, including the maintainer.

Read [`AGENTS.md`](AGENTS.md) first - it is the contract for humans and agents
and covers the worktree/stacking workflow, language and formatting rules, and
testing.

## Review model

Every change to `main` goes through a pull request. No direct pushes, no force
pushes, and a PR must be up to date with `main` before it merges.

- **Required status checks must be green** — `Scaffold checks`,
  `Python lint & format checks`, `Pytest (hermetic)`, `Packaging smoke`,
  `Shellcheck` (see the table below). This is the hard merge gate;
  `enforce_admins` is off so a green run is the only thing routinely bypassed.
- **Agent review runs on every PR head.** `mergestorm-vortex` reviews each push;
  [`.cursor/rules/pr-ship-and-review.mdc`](.cursor/rules/pr-ship-and-review.mdc)
  requires an on-thread human reply before any review thread is resolved.
  Threads are addressed before merge.
- **Code-owner review.** [`.github/CODEOWNERS`](.github/CODEOWNERS) is
  `* @thehcma` with `require_code_owner_reviews` on, so GitHub requests
  @thehcma on every PR and records who reviewed. `required_approving_review_count`
  is `0` — a solo maintainer cannot approve their own PR, and a block nobody can
  clear only forces admin-override merges. External contributions get @thehcma's
  review by practice; only collaborators can merge, and the checks gate it.
  Rationale and the "second contributor" trigger to raise it back to 1:
  [`docs/DECISIONS.md`](docs/DECISIONS.md) (D2).

## Static analysis and scanning

Run locally with
[`pre-pr-checks`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/dev/pre-pr-checks)
(or the individual [`.github/ci/*`](.github/ci) scripts). CI
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same gates.

The **required status checks** on `main` are `Scaffold checks`,
`Python lint & format checks`, `Pytest (hermetic)`, `Packaging smoke`, and
`Shellcheck` - a red one of those blocks the merge. `Secret Scan`, `Guard`, and
workflow-lint run on every PR but are advisory; a code owner still reviews the
diff before merge, so a real finding is caught there.

| Tool | What it catches | Where it runs | Required on `main`? |
|------|-----------------|---------------|---------------------|
| `ruff check` | Lint: pyflakes (F), pycodestyle (E), import order (I), pyupgrade (UP) - AST-based ([`[tool.ruff]`](pyproject.toml)) | [`.github/ci/python-static`](.github/ci/python-static) → [`ci.yml` `python-static`](.github/workflows/ci.yml) | `Python lint & format checks` - **yes** |
| `ruff format --check` | Formatting drift | [`.github/ci/python-static`](.github/ci/python-static) | `Python lint & format checks` - **yes** |
| `pyright` (basic) | Type errors, unresolved names, bad signatures - AST + type graph ([`[tool.pyright]`](pyproject.toml)) | [`.github/ci/python-static`](.github/ci/python-static) | `Python lint & format checks` - **yes** |
| [`.github/ci/assert-uv-lock-version`](.github/ci/assert-uv-lock-version) | `uv.lock` project version drift vs [`pyproject.toml`](pyproject.toml) | [`.github/ci/python-static`](.github/ci/python-static) | `Python lint & format checks` - **yes** |
| `pytest -m 'not live'` | Behaviour, offline/mocked | [`.github/ci/pytest-hermetic`](.github/ci/pytest-hermetic) → [`ci.yml` `test`](.github/workflows/ci.yml) | `Pytest (hermetic)` - **yes** |
| [`test_packaging`](test_packaging) | Broken entry point / missing package data in the built wheel | [`ci.yml` `packaging`](.github/workflows/ci.yml) | `Packaging smoke` - **yes** |
| `shellcheck -S info` | Shell script defects | [`.github/ci/shellcheck`](.github/ci/shellcheck) → [`ci.yml` `shellcheck`](.github/workflows/ci.yml) | `Shellcheck` - **yes** |
| `gitleaks` | Committed secrets / tokens | [`.github/ci/secret-scan`](.github/ci/secret-scan) (org-canonical) → [`ci.yml` `secret-scan`](.github/workflows/ci.yml) | no - advisory, runs every PR |
| `bash -n` | Shell syntax | [`pre-pr-checks`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/dev/pre-pr-checks) | local gate |
| `actionlint` | GitHub Actions workflow errors | `pre-pr-checks` / `github-repo-lint` | local gate |
| [`github-repo-lint`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/github-repo-lint) | Org repo-practice compliance (see below) | `pre-pr-checks` (`repo-practices-lint`) | local gate |
| `pip-audit` | Known CVEs in dependencies | [`.github/workflows/cve-check.yml`](.github/workflows/cve-check.yml) (daily) | no - files a `security/cve` issue |
| Dependabot | Outdated dependencies, 10-day cooldown | [`.github/dependabot.yml`](.github/dependabot.yml) + [`dependabot-auto-merge.yml`](.github/workflows/dependabot-auto-merge.yml) | opens PRs |
| `mergestorm-vortex` | Correctness / design review, AST-aware | every PR head | not a status check - threads addressed before merge |
| [`scripts/verify-pypi-release`](scripts/verify-pypi-release) | Published artifact installs and reports the right version/commit | [`release-please.yml` `publish-pypi`](.github/workflows/release-please.yml) | gates the release job |

New behaviour needs tests. Do not merge a red suite. Live Graph tests
(`-m live`) run on an operator machine, never in CI - see
[`.cursor/rules/local-live-graph-tests.mdc`](.cursor/rules/local-live-graph-tests.mdc).

## Governance tooling from repository-helpers

The compliance gates, the coding/workflow rules, and the dev scripts above are
**not in this repo**. They are versioned in
[`the-hcma/repository-helpers`](https://github.com/the-hcma/repository-helpers)
and re-synced here (`AGENTS.md` › "Repository practices / lint"). A blumkin
contributor uses:

- [`start-development`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/dev/start-development)
  - creates the per-stack worktree at session start.
- [`pre-pr-checks`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/dev/pre-pr-checks)
  - the local gate (must be green before submit); runs the `.github/ci/*`
    scripts plus `repo-practices-lint` and verified-commits.
- [`submit-stack`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/dev/submit-stack)
  / the [`gh-stack`](https://github.com/the-hcma/repository-helpers/blob/main/.cursor/skills/gh-stack/SKILL.md)
  skill - stacked-PR submit (`.github/stacking-tool` = `gh-stack`).
- [`wait-for-agent-review`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/wait-for-agent-review)
  / the [`ship-and-review`](https://github.com/the-hcma/repository-helpers/blob/main/.cursor/skills/ship-and-review/SKILL.md)
  skill - the agent-review loop (reply-before-resolve, CI wait).
- [`github-repo-lint`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/github-repo-lint)
  - enforces branch-protection shape, required workflows, CODEOWNERS, Dependabot
    cooldown, and that the [`.cursor/rules/*.mdc`](.cursor/rules) here still
    match the [canonical templates](https://github.com/the-hcma/repository-helpers/tree/main/scripts/lib/repo-practices-cursor).
    Re-run `github-repo-lint --suggest` after touching `.github/workflows/**`,
    `.github/dependabot.yml`, or the stacking marker
    (`.cursor/rules/repo-practices-after-config-change.mdc`).

The [`.cursor/rules/*.mdc`](.cursor/rules) themselves (secret handling,
no-third-party-side-effects, remote timeouts, commit identity, stacking, the
pre-PR gate) are consumer copies of those templates - edit them upstream, not
here. `.github/ci/secret-scan` is likewise byte-identical to
[`scripts/lib/ci-secret-scan`](https://github.com/the-hcma/repository-helpers/blob/main/scripts/lib/ci-secret-scan);
sync it with `github-repo-lint --apply-fix`, never by hand.
See [`SECURITY.md` › Governance tooling](SECURITY.md#governance-tooling-repository-helpers)
for the same list framed as security controls.

## Commits and PRs

- [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`,
  `chore:`, `docs:`, `test:`, `refactor:`. `feat:` / `fix:` drive releases.
- Attribution is the committer only - no `Co-authored-by` / agent trailers
  ([`.cursor/rules/git-commit-identity.mdc`](.cursor/rules/git-commit-identity.mdc)).
- Stacking backend is `gh-stack` ([`.github/stacking-tool`](.github/stacking-tool));
  worktree-per-stack via `start-development`. Do not use Graphite.
  ([`.cursor/rules/stacking-tool.mdc`](.cursor/rules/stacking-tool.mdc))
- Never verify a change by running a skill that notifies other people
  ([`.cursor/rules/no-third-party-side-effects.mdc`](.cursor/rules/no-third-party-side-effects.mdc)).
- Never put a secret in a commit, log, PR body, or review reply
  ([`.cursor/rules/no-secret-exposure.mdc`](.cursor/rules/no-secret-exposure.mdc)).

## Design and decisions

Design rationale lives in [`PLAN.md`](PLAN.md); milestone retrospectives in
`RETROSPECTIVE-M*.md`; standing decisions in
[`docs/DECISIONS.md`](docs/DECISIONS.md). A change that alters the CLI contract,
the auth model, or the release/security posture should update the relevant one
in the same PR.
