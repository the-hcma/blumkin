# Releasing blumkin

Releases are automated with
[Release Please](https://github.com/googleapis/release-please) and PyPI trusted
publishing. Do not edit the package version or create release tags by hand.

## Release flow

1. Merge changes to `main` with
   [Conventional Commits](https://www.conventionalcommits.org/). `feat:` cuts a
   minor release, `fix:` a patch, and a `!` / `BREAKING CHANGE:` a major.
2. Release Please opens or updates its release PR with the next version,
   `CHANGELOG.md`, `pyproject.toml`, and `uv.lock`.
3. Review and merge the release PR.
4. Release Please creates the version tag and GitHub release.
5. The `Release Please` workflow's `Publish PyPI` job embeds the build metadata,
   builds the wheel and sdist, publishes them to PyPI over OpenID Connect (no
   token), then runs `scripts/verify-pypi-release` — which polls PyPI for the
   new version, installs the real artifact into an isolated venv **and** via a
   real `pipx install` (its own venv layout and console-script shims), and
   asserts `blumkin --version` reports the released version and a known
   commit both ways. No manual `pipx install` check is needed to trust a
   release (issue #142). It then runs `scripts/verify-pipx-upgrade`, which
   pipx-installs the *previous* published version and runs `blumkin upgrade`
   against it, asserting it lands on the version just published (issue #143).
   That needs two published releases, so it is a no-op — not a failure — on
   the very first release.

Release Please scans every commit since the previous tag. Keep squash-commit
subjects conventional, and keep the repo's `squash_merge_commit_message` setting
as `BLANK` so repeated PR-body commit lines do not become duplicate changelog
entries.

## Trusted publisher setup (one time)

An operator must configure both sides before the first publish. No PyPI API
token or repository secret is involved — the workflow's `id-token: write`
permission plus the protected `pypi` environment mint the short-lived
credential.

1. **GitHub** — repository Settings → Environments → create an environment named
   `pypi` (equivalently
   `gh api -X PUT repos/the-hcma/blumkin/environments/pypi`). Required
   reviewers and branch restrictions are optional.
2. **PyPI** — the `blumkin` project does not exist yet, so add a *pending*
   trusted publisher: PyPI account → Publishing → "Add a pending publisher":
   - PyPI Project Name: `blumkin`
   - Owner: `the-hcma`
   - Repository name: `blumkin`
   - Workflow name: `release-please.yml`
   - Environment name: `pypi`
3. The first successful `Publish PyPI` run creates the project and converts the
   pending publisher into a normal one.

## Install a published release

```bash
pipx install blumkin
pipx ensurepath   # first pipx install only
blumkin --version
```

Move to a newer release with:

```bash
blumkin upgrade
```

which wraps `pipx upgrade blumkin` and prints the version and commit you moved
from and to. Bare `pipx upgrade blumkin` also works but cannot tell you whether
`PATH` still resolves to a dev checkout.

The editable dev install (`uv tool install -e .` from a clone) stays the path
for working on blumkin itself.

## Rollback

A published PyPI release cannot be replaced - the version is immutable. To
recover from a bad release:

1. **Patch forward (preferred).** Land a `fix:` PR on `main`, merge the release
   PR it triggers, and let the pipeline publish the next patch version.
   `blumkin upgrade` moves users to it.
2. **Yank the bad version** if it is actively harmful (crashes on start, leaks,
   installs broken): on PyPI, project → Manage → Releases → the version →
   *Yank*. Yanked versions stay installable by exact pin but are skipped by
   `pipx install blumkin` / `pip install blumkin`. Yank, then patch forward.
3. **Revert the code.** `git revert` the offending commit(s) on `main` via a PR
   (`fix:` or `revert:`); that PR's release publishes the reverted state as a
   new version.

Do not delete the tag or the GitHub release - Release Please tracks state from
them, and deleting one desyncs the next run. If a release must be fully undone,
revert on `main` and cut a new version rather than rewriting history.

## Security issues

A vulnerability fix is a normal `fix:` PR and release. For severity targets and
private reporting, see [`../SECURITY.md`](../SECURITY.md). If the fix must ship
before the next routine release, merge its release PR immediately after the fix
PR lands.
