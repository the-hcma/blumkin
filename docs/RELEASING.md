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
   new version, installs the real artifact into an isolated venv, and asserts
   `blumkin --version` reports the released version and a known commit.

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

which prints the version and commit you are on and the one it upgraded to. Bare
`pipx upgrade blumkin` also works but cannot tell you whether `PATH` still
resolves to a dev checkout.

The editable dev install (`uv tool install -e .` from a clone) stays the path
for working on blumkin itself.
