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
5. The `Publish PyPI` job runs in the `pypi` environment, which **waits for
   @thehcma to approve the deployment** (Actions run page → Review deployments).
   Approve it.
6. The job then embeds the build metadata, builds the wheel and sdist, publishes
   them to PyPI over OpenID Connect (no token), then runs
   `scripts/verify-pypi-release` — which polls PyPI for the new version, installs
   the real artifact into an isolated venv **and** via a real `pipx install` (its
   own venv layout and console-script shims), and asserts `blumkin --version`
   reports the released version and a known commit both ways. No manual
   `pipx install` check is needed to trust a release (issue #142).

Release Please scans every commit since the previous tag. Keep squash-commit
subjects conventional, and keep the repo's `squash_merge_commit_message` setting
as `BLANK` so repeated PR-body commit lines do not become duplicate changelog
entries.

## Trusted publisher setup (one time)

An operator must configure both sides before the first publish. No PyPI API
token or repository secret is involved — the workflow's `id-token: write`
permission plus the protected `pypi` environment mint the short-lived
credential.

1. **GitHub** — repository Settings → Environments → environment `pypi`, with
   **@thehcma as a required reviewer** and deployments restricted to the `main`
   branch and `blumkin-v*` tags. So every publish pauses for a human, and a
   publish cannot be triggered from an arbitrary ref.
2. **PyPI** — the `blumkin` project does not exist yet, so add a *pending*
   trusted publisher: PyPI account → Publishing → "Add a pending publisher":
   - PyPI Project Name: `blumkin`
   - Owner: `the-hcma`
   - Repository name: `blumkin`
   - Workflow name: `release-please.yml`
   - Environment name: `pypi`
3. The first successful `Publish PyPI` run creates the project and converts the
   pending publisher into a normal one.

## Pi npm package (`integrations/pi`)

`@the-hcma/blumkin-pi-skill` (the [pi.dev](https://pi.dev) package wrapping
blumkin's Agent Skill, see [`../docs/agent-integration.md`](agent-integration.md#pi))
is a second, independently versioned Release Please package in the same
manifest, scoped to `integrations/pi/**`. It follows the same flow as above:
a `feat`/`fix` commit touching that path gets its own release PR
(`CHANGELOG.md` under `integrations/pi/`), its own tag
(`blumkin-pi-skill-v<version>`), and its own `Publish npm` job gated on the
protected `npm` environment (required reviewer, `main` + `blumkin-pi-skill-v*`
only) — mirroring `pypi` above, including OIDC trusted publishing (no npm
token or repository secret; `id-token: write` plus the environment mint the
credential).

**One-time bootstrap, before trusted publishing can take over:** unlike
PyPI, npm has no "pending trusted publisher" for a package that has never
been published — the trust relationship can only be attached to a package
that already exists on the registry. So the very first publish of
`@the-hcma/blumkin-pi-skill` has to happen manually:

1. `cd integrations/pi && npm login && npm publish --access public` (creates
   the package on the registry; runs the `prepack` script that materializes
   `skills/blumkin/` from `.agents/skills/blumkin/`).
2. On npmjs.com: package → Settings → Trusted Publisher → add GitHub Actions,
   with Owner `the-hcma`, Repository `blumkin`, Workflow `release-please.yml`,
   Environment `npm`.
3. From the next release onward, the `Publish npm` job in this workflow
   takes over — same as `Publish PyPI` does once its pending publisher
   converts.

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

which detects the install method (pipx here) and shells `pipx upgrade blumkin`,
printing the version and commit you moved from and to. `blumkin upgrade` also
handles a `uv tool` install (`uv tool upgrade blumkin`) and an editable
`-e <path>` checkout (git pull + `--force` reinstall, printed or run with
`--yes`); an unmanaged install is reported, not touched.

From the second published release onward, `test_packaging` (CI *Packaging smoke*)
also runs the real round-trip: `pipx install` the second-newest published
release into a custom `PIPX_BIN_DIR`, run *its* `blumkin upgrade`, and assert it
moved to the newest release with the managed path under that bin dir (issues
#143 / #239). The check tolerates both JSON shapes since the binary under test
is the previous release; the `install_method == "pipx"` assertion only fires
once a release ships #239.

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
