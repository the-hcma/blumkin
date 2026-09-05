# Decisions

Standing decisions for blumkin and where the reasoning lives. Add an entry when
a choice would otherwise have to be re-derived from the code or from a PR
thread. Keep entries short; link the PR / issue / design doc.

## Design artifacts

| Artifact | What it is |
|----------|------------|
| [`PLAN.md`](../PLAN.md) | The CLI design: surface, exit codes, auth, safety model, phased scope |
| [`RETROSPECTIVE-M1.md`](../RETROSPECTIVE-M1.md) | M1 ship retrospective ([#11](https://github.com/the-hcma/blumkin/issues/11)) |
| [`HANDOFF.md`](../HANDOFF.md) | Session-to-session context handoff |
| [`docs/agent-integration.md`](agent-integration.md) | How agents call blumkin; the frozen `skills list --json` contract |
| [`docs/RELEASING.md`](RELEASING.md) | Release flow, trusted publishing, rollback |
| [`SECURITY.md`](../SECURITY.md) / [`docs/SECURITY-AT-A-GLANCE.md`](SECURITY-AT-A-GLANCE.md) | Security policy and one-page summary |

## Decision log

### D1 - Public repository

blumkin is a public repo. It contains no secrets, no proprietary code, and no
third-party data; it is a thin wrapper over documented Microsoft Graph and
Google Workspace APIs. Public means the org's `repository-helpers` practices,
CI, Dependabot, and trusted publishing all apply without private-repo caveats,
and `pipx install blumkin` works for anyone. The tenant-specific pieces (client
ids, tenant ids) live only in each operator's `~/.config/blumkin/`.

### D2 - Personal org (`the-hcma`), single code owner

The repo lives in a personal GitHub org (`the-hcma`, no other members) with
@thehcma as the sole code owner. This is a personal productivity tool, not a
team service, so the review gate is tuned for one person rather than a team:

- `main` cannot be pushed to directly; every change is a PR that must be **up to
  date** with `main` before it merges. No force pushes, no branch deletion.
- **Required status checks** (`Scaffold checks`, `Python lint & format checks`,
  `Pytest (hermetic)`, `Packaging smoke`, `Shellcheck`) must be green - this is
  the hard merge gate, and `enforce_admins` stays off so it is the *only* thing
  the maintainer routinely bypasses when nothing is red.
- Agent review (`mergestorm-vortex`) runs on every PR head; its threads are
  addressed and resolved before merge (`.cursor/rules/pr-ship-and-review.mdc`).
- [`.github/CODEOWNERS`](../.github/CODEOWNERS) is `* @thehcma` and
  `require_code_owner_reviews` is on, so GitHub requests @thehcma on every PR
  and records who reviewed. `required_approving_review_count` is **0**: a lone
  maintainer cannot approve their own PR, and a hard approval gate with nobody
  able to satisfy it just means merging by admin override every time, which
  weakens the status-check gate too. `dismiss_stale_reviews` stays on.
- External contributions get @thehcma's review by practice (and the requested-
  reviewer prompt), not by a hard block - only collaborators can merge at all,
  and the checks still gate every merge.
- **When a second regular contributor appears:** add them as a reviewer and set
  `required_approving_review_count` back to 1 - then the code-owner gate is a
  real block again with someone able to satisfy it.

### D3 - Delegated Graph auth only

No app-only permissions, no client secret for Microsoft flows, no service
account. blumkin acts as the signed-in user with their consent. Rationale and
the full model: [`PLAN.md`](../PLAN.md) sections 1-2 and
[`docs/SECURITY-AT-A-GLANCE.md`](SECURITY-AT-A-GLANCE.md).

### D4 - PyPI trusted publishing (OIDC), release-please

Releases are automated from Conventional Commits; publishing uses a GitHub OIDC
trusted publisher with no stored PyPI token. Nobody hand-edits a version.
Details and one-time setup: [`docs/RELEASING.md`](RELEASING.md). Introduced in
[#54](https://github.com/the-hcma/blumkin/issues/54) (PRs #135-138).

### D5 - `gh-stack` for stacked PRs

Stacking backend is `gh-stack` (`.github/stacking-tool`), not Graphite. Keeps
each layer of a change independently reviewable. See
[`.cursor/rules/stacking-tool.mdc`](../.cursor/rules/stacking-tool.mdc).

### D6 - No MCP server in v1

Agents shell out to `blumkin --json`; there is no MCP server. Reasoning:
[`PLAN.md`](../PLAN.md) section 6.1.

### D7 - GitHub-native security hardening

Applied on `the-hcma/blumkin` ([#155](https://github.com/the-hcma/blumkin/issues/155)),
ahead of `github-repo-lint` learning to enforce them
([repository-helpers#588](https://github.com/the-hcma/repository-helpers/issues/588)):

- **Secret scanning + push protection** on. Push protection blocks a recognised
  secret at `git push`, before the CI `gitleaks` gate would ever see it.
- **Dependabot alerts + security updates** and **private vulnerability
  reporting** on (the latter is what `SECURITY.md`'s advisory link needs).
- **Actions**: `allowed_actions: selected` with an explicit allowlist; default
  workflow token read-only and cannot approve PRs; every `uses:` in every
  workflow SHA-pinned (so `sha_pinning_required` can be turned on next).
- **`pypi` environment**: @thehcma is a required reviewer and deploys are
  restricted to `main` / `blumkin-v*`, so every publish pauses for a human and
  cannot run from an arbitrary ref.

Not done yet: `sha_pinning_required: true` (flip once all workflows are pinned -
this PR does that). Not done (deliberate): GHAS-only
`secret_scanning_validity_checks` / `non_provider_patterns` (need the paid
add-on); consolidating the classic branch-protection + `protect-main` ruleset
overlap; `required_conversation_resolution` (would force every agent-review
thread resolved before merge - revisit).

### D8 - Google Meet / transcription stays stubbed for `provider = "google"`

Layers A-F of [#89](https://github.com/the-hcma/blumkin/issues/89) brought
`provider = "google"` to Microsoft parity for calendar, mail, people, and chat.
Layer G - `meeting get` / `meeting transcription` - is **deliberately left
stubbed**: both raise a clear `not supported for provider=google` error.

Reasoning: the Meet REST surface (`conferenceRecords` / `conferenceRecords.
transcripts`) needs new OAuth scopes (`meetings.space.readonly` plus a
transcript scope) and a fresh browser consent, and transcripts only exist when a
meeting was recorded through Google's own artifacts config - a large, low-value
addition for a personal CLI whose Meet usage is ad hoc. The Microsoft
`meeting.*` skills themselves are gated on the WO1162425 add-on and rarely used.

If Meet transcript access becomes necessary, reopen as a new issue rather than
under #89. Layer H (this change) adds the `live_google` pytest marker and
refreshes the support matrix, closing #89.
