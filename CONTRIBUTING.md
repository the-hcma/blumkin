# Contributing to blumkin

blumkin is a personal tool with a single maintainer / code owner (@thehcma).
Contributions are welcome as issues and pull requests; the bar is the same for
everyone, including the maintainer.

Read [`AGENTS.md`](AGENTS.md) first - it is the contract for humans and agents
and covers the worktree/stacking workflow, language and formatting rules, and
testing.

## Review model

Every change to `main` goes through a pull request. No direct pushes.

- **Code-owner review is required.** `main` branch protection has
  `require_code_owner_reviews`, `required_approving_review_count >= 1`, and
  `require_last_push_approval`, and [`.github/CODEOWNERS`](.github/CODEOWNERS) is
  `* @thehcma`. So @thehcma must approve every PR, and any push after approval
  re-requests review.
- **Agent review runs on every PR head.** `mergestorm-vortex` reviews each push;
  `.cursor/rules/pr-ship-and-review.mdc` requires an on-thread human reply
  before any review thread is resolved.
- **Required status checks** must be green (see the table below).
- The maintainer's own PRs: GitHub does not allow self-approval, so the
  maintainer merges their own PRs by admin once agent review is clear and all
  checks pass. Every PR from anyone else waits for an explicit @thehcma
  approval. See [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Static analysis and scanning

Run locally with `~/work/ai/repository-helpers/scripts/dev/pre-pr-checks`
(or the individual `.github/ci/*` scripts). CI runs the same gates.

The **required status checks** on `main` are `Scaffold checks`,
`Python lint & format checks`, `Pytest (hermetic)`, `Packaging smoke`, and
`Shellcheck` - a red one of those blocks the merge. `Secret Scan`, `Guard`, and
workflow-lint run on every PR but are advisory; a code owner still reviews the
diff before merge, so a real finding is caught there.

| Tool | What it catches | CI check | Required on `main`? |
|------|-----------------|----------|---------------------|
| `ruff check` | Lint: pyflakes (F), pycodestyle (E), import order (I), pyupgrade (UP) - AST-based | `Python lint & format checks` | yes |
| `ruff format --check` | Formatting drift | `Python lint & format checks` | yes |
| `pyright` (basic) | Type errors, unresolved names, bad signatures - AST + type graph | `Python lint & format checks` | yes |
| `.github/ci/assert-uv-lock-version` | `uv.lock` project version drift vs `pyproject.toml` | `Python lint & format checks` | yes |
| `pytest -m 'not live'` | Behaviour, offline/mocked | `Pytest (hermetic)` | yes |
| `test_packaging` | Broken entry point / missing package data in the built wheel | `Packaging smoke` | yes |
| `shellcheck -S info` | Shell script defects | `Shellcheck` | yes |
| `gitleaks` | Committed secrets / tokens | `Secret Scan` | no - advisory, runs every PR |
| `bash -n` | Shell syntax | (local `pre-pr-checks`) | local gate |
| `actionlint` | GitHub Actions workflow errors | (local `pre-pr-checks` / repo-lint) | local gate |
| `github-repo-lint` | Org repo-practice compliance | (local `pre-pr-checks`) | local gate |
| `pip-audit` | Known CVEs in dependencies | `.github/workflows/cve-check.yml` (daily) | no - files a `security/cve` issue |
| Dependabot | Outdated dependencies (10-day cooldown) | scheduled | opens PRs |
| `mergestorm-vortex` | Correctness / design review, AST-aware | every PR head | not a status check - review threads must be addressed before a code owner approves |
| `verify-pypi-release` | Published artifact installs and reports the right version/commit | `Publish PyPI` job | gates the release job |

New behaviour needs tests. Do not merge a red suite. Live Graph tests
(`-m live`) run on an operator machine, never in CI - see
[`.cursor/rules/local-live-graph-tests.mdc`](.cursor/rules/local-live-graph-tests.mdc).

## Commits and PRs

- [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`,
  `chore:`, `docs:`, `test:`, `refactor:`. `feat:` / `fix:` drive releases.
- Attribution is the committer only - no `Co-authored-by` / agent trailers
  (`.cursor/rules/git-commit-identity.mdc`).
- Stacking backend is `gh-stack` (`.github/stacking-tool`); worktree-per-stack
  via `start-development`. Do not use Graphite.
- Never verify a change by running a skill that notifies other people
  (`.cursor/rules/no-third-party-side-effects.mdc`).

## Design and decisions

Design rationale lives in [`PLAN.md`](PLAN.md); milestone retrospectives in
`RETROSPECTIVE-M*.md`; standing decisions in
[`docs/DECISIONS.md`](docs/DECISIONS.md). A change that alters the CLI contract,
the auth model, or the release/security posture should update the relevant one
in the same PR.
