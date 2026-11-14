# Releasing blumkin

Releases are automated with [Release Please](https://github.com/googleapis/release-please) and PyPI trusted publishing. Do not edit the package version or create release tags by hand.

## Release flow

1. Merge changes to `main` with [Conventional Commits](https://www.conventionalcommits.org/). `feat:` cuts a minor release, `fix:` a patch, and a `!` / `BREAKING CHANGE:` a major.
2. Release Please opens or updates its release PR with the next version, `CHANGELOG.md`, `pyproject.toml`, and `uv.lock`. Until the Conveyor App token of [#197](https://github.com/the-hcma/blumkin/issues/197) is wired in, the workflow cannot open that PR itself: open it by hand, see [Opening the release PR by hand](#opening-the-release-pr-by-hand).
3. Review and merge the release PR.
4. Release Please creates the version tag and GitHub release.
5. The `Publish PyPI` job runs in the `pypi` environment, which **waits for @thehcma to approve the deployment** (Actions run page → Review deployments). Approve it.
6. The job then embeds the build metadata, builds the wheel and sdist, publishes them to PyPI over OpenID Connect (no token), then runs `scripts/verify-pypi-release` — which polls PyPI for the new version, installs the real artifact into an isolated venv **and** via a real `pipx install` (its own venv layout and console-script shims), and asserts `blumkin --version` reports the released version and a known commit both ways. No manual `pipx install` check is needed to trust a release (issue #142).

Release Please scans every commit since the previous tag. Keep squash-commit subjects conventional, and keep the repo's `squash_merge_commit_message` setting as `BLANK` so repeated PR-body commit lines do not become duplicate changelog entries.

## Opening the release PR by hand

The `Release Please` workflow fails on every push to `main` with "GitHub Actions is not permitted to create or approve pull requests": that setting is off on purpose (`docs/DECISIONS.md` D7), and the run stops after it has built the release branch and before it opens the PR. Until [#197](https://github.com/the-hcma/blumkin/issues/197) wires in a non-default token, open the PR with your own credentials.

1. Preview it: `npx release-please@16 release-pr --token "$(gh auth token)" --repo-url the-hcma/blumkin --target-branch main --config-file release-please-config.json --manifest-file .release-please-manifest.json --dry-run` prints the version, the changelog and the files it would change.
2. Run the same command without `--dry-run`. It opens the release PR (title `chore: release main`, label `autorelease: pending`). It can end with a harmless `failed to run git` line when it is not run inside a clone.
3. Run the workflow once on the PR: `gh workflow run release-please.yml --ref main`. Its `Release PR CI` and `Update uv.lock` jobs then run against the PR. The run also rewrites the release branch with a commit authored by `github-actions[bot]`. GitHub signs that commit, but a push made with the Actions token does not start the PR's required checks, so the PR shows only skipped checks and stays blocked.
4. Recreate the release commit under your own identity. In a worktree on the release branch, first confirm that the branch sits directly on the current tip of `main`: `git fetch origin`, then `git merge-base origin/main origin/release-please--branches--main` must equal `git rev-parse origin/main` (if `main` has moved, run the workflow again so Release Please rebuilds the PR on it, and do not carry on). The branch holds the release commit and, when the lockfile job changed the file, a second `chore: update uv.lock` commit. Only after that check, `git reset --soft origin/main`, commit once with `git commit -S` and the message `chore: release main`, confirm `git rev-parse HEAD^{tree}` equals the tree of the branch tip you replaced and `git rev-parse HEAD^` equals `git rev-parse origin/main`, then `git push --force-with-lease=<branch>:<old tip sha>` with the full sha. This force-pushes the release branch, which is allowed: Release Please rewrites that branch itself on every run, and the no-force-push rule of `CONTRIBUTING.md` is about `main`. The PR now shows a Verified commit under your name and its required checks run. Do not edit the PR title or body: Release Please reads both when the PR merges (`post-pr-submission-checks` reports that the body has no `## Summary`, which is expected for this PR).
5. When the required checks pass, merge with `"${REPOSITORY_HELPERS_DIR:-$HOME/work/ai/repository-helpers}/scripts/dev/merge-stack" --pr <n>` (the helper lives in repository-helpers, not in this repository). The merge creates the tag and the GitHub release.

## The build legs and the PyPI publish

`Publish PyPI` needs both `Build distributions` legs. `ubuntu-latest` builds the sdist and the `py3-none-any` wheel. `macos-latest` builds the macOS wheel that bundles the `blumkin-agent` binary: it builds with `MACOSX_DEPLOYMENT_TARGET=14.0`, so the wheel stays `macosx_14_0_universal2` whatever macOS the runner image has, and a step fails the build before anything is uploaded unless `dist/` holds exactly that one macOS wheel (a published PyPI file cannot be replaced). `tests/test_macos_wheel_tag_unit.py` pins how hatchling names that tag.

If a leg fails or is cancelled, `Publish PyPI` is skipped: the tag and the GitHub release exist and PyPI does not have the version (the 1.14.0 macOS leg was cancelled with "The configuration 'macos-14-arm64-us-default' is not supported" when the runner label stopped being provided). Fix the cause on `main` with a commit that does not release by itself (`ci:` or `docs:`, so no new release PR opens). Before dispatching, run `git fetch origin` right then (a stale `origin/main` can hide commits that landed on the remote `main`, which is what gets built) and check what `main` holds since the tag: `git log --oneline blumkin-v<version>..origin/main` must list only the recovery changes, because the forced build publishes the HEAD of `main` under the version in its `pyproject.toml`, so a feature merged after the tag would be published under the tagged version, and that version must be the tagged one. Then run `gh workflow run release-please.yml --ref main -f force_publish_pypi=true`. The embedded build commit is the HEAD of `main`, not the tag's commit. `scripts/verify-pypi-release` then checks that PyPI serves the version and that `blumkin --version` reports it with a known commit from the installed wheel and from a real `pipx install`; it does not compare that commit with `main`, and because the publish step uses `skip-existing`, a file that is already on PyPI is left in place, not replaced. Approve the `pypi` deployment as described in the Release flow above and wait for the verify step to pass.

## Trusted publisher setup (one time)

An operator configures both sides once. No PyPI API token or repository secret is involved — the workflow's `id-token: write` permission plus the protected `pypi` environment mint the short-lived credential.

1. **GitHub** — repository Settings → Environments → environment `pypi`, with **@thehcma as a required reviewer** and deployments restricted to the `main` branch and `blumkin-v*` tags. So every publish pauses for a human, and a publish cannot be triggered from an arbitrary ref.
2. **PyPI** — the `blumkin` project exists, and its Publishing settings hold the trusted publisher the releases publish through; if it is ever removed, add it again there with:
   - Owner: `the-hcma`
   - Repository name: `blumkin`
   - Workflow name: `release-please.yml`
   - Environment name: `pypi`

## Pi npm package (`integrations/pi`)

`@the-hcma/blumkin-pi-skill` (the [pi.dev](https://pi.dev) package wrapping blumkin's Agent Skill, see [`../docs/agent-integration.md`](agent-integration.md#pi)) is a second, independently versioned Release Please package in the same manifest, scoped to `integrations/pi/**`. It follows the same flow as above, except that the release configuration does not set `separate-pull-requests`: a `feat`/`fix` commit touching that path adds a Pi release to the one combined release PR, next to any pending `blumkin` release, and merging that PR releases every package that has a pending release. Each package then gets its own changelog (`CHANGELOG.md` under `integrations/pi/`), its own tag (`blumkin-pi-skill-v<version>`), and its own `Publish npm` job gated on the protected `npm` environment (required reviewer, `main` + `blumkin-pi-skill-v*` only) — mirroring `pypi` above, including OIDC trusted publishing (no npm token or repository secret; `id-token: write` plus the environment mint the credential).

**One-time bootstrap, before trusted publishing can take over:** unlike PyPI, npm has no "pending trusted publisher" for a package that has never been published — the trust relationship can only be attached to a package that already exists on the registry. So the very first publish of `@the-hcma/blumkin-pi-skill` has to happen manually:

1. `cd integrations/pi && npm login && npm publish --access public` (creates the package on the registry; runs the `prepack` script that materializes `skills/blumkin/` from `.agents/skills/blumkin/`).
2. On npmjs.com: package → Settings → Trusted Publisher → add GitHub Actions, with Owner `the-hcma`, Repository `blumkin`, Workflow `release-please.yml`, Environment `npm`.
3. From the next release onward, the `Publish npm` job in this workflow takes over — same as `Publish PyPI` does once its pending publisher converts.

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

which detects the install method (pipx here) and shells `pipx upgrade blumkin`, printing the version and commit you moved from and to. `blumkin upgrade` also handles a `uv tool` install (`uv tool upgrade blumkin`) and an editable `-e <path>` checkout (git pull + `--force` reinstall, printed or run with `--yes`); an unmanaged install is reported, not touched.

From the second published release onward, `test_packaging` (CI *Packaging smoke*) also runs the real round-trip: `pipx install` the second-newest published release into a custom `PIPX_BIN_DIR`, run *its* `blumkin upgrade`, and assert it moved to the newest release with the managed path under that bin dir (issues #143 / #239). The check tolerates both JSON shapes since the binary under test is the previous release; the `install_method == "pipx"` assertion only fires once a release ships #239.

The editable dev install (`uv tool install -e .` from a clone) stays the path for working on blumkin itself.

## Rollback

A published PyPI release cannot be replaced - the version is immutable. To recover from a bad release:

1. **Patch forward (preferred).** Land a `fix:` PR on `main`, merge the release PR it triggers, and let the pipeline publish the next patch version. `blumkin upgrade` moves users to it.
2. **Yank the bad version** if it is actively harmful (crashes on start, leaks, installs broken): on PyPI, project → Manage → Releases → the version → *Yank*. Yanked versions stay installable by exact pin but are skipped by `pipx install blumkin` / `pip install blumkin`. Yank, then patch forward.
3. **Revert the code.** `git revert` the offending commit(s) on `main` via a PR (`fix:` or `revert:`); that PR's release publishes the reverted state as a new version.

Do not delete the tag or the GitHub release - Release Please tracks state from them, and deleting one desyncs the next run. If a release must be fully undone, revert on `main` and cut a new version rather than rewriting history.

## Security issues

A vulnerability fix is a normal `fix:` PR and release. For severity targets and private reporting, see [`../SECURITY.md`](../SECURITY.md). If the fix must ship before the next routine release, merge its release PR immediately after the fix PR lands.
