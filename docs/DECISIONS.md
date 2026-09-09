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

### D6 - MCP via a thin stdio adapter; CLI stays the source of truth

`blumkin mcp serve` ([#113](https://github.com/the-hcma/blumkin/issues/113))
exposes every provider-backed skill as a typed MCP tool, generated from the same
catalog the CLI publishes and dispatched through the same `run_skill` /
`classify_exception` path - no second Graph implementation and no drift surface.
It is an ephemeral stdio process the host spawns and reaps per session (Claude
Code, Cursor CLI, GitHub Copilot CLI are all MCP-native stdio clients now), not a
daemon, and it reuses the CLI's on-disk token cache, so there is no new auth
surface. `mcp` is an optional extra (`pipx install 'blumkin[mcp]'`); the CLI-only
verbs (`auth *`, `doctor`, `skills *`, `mail signature`, `mcp serve`) are not
exposed; every tool whose CLI form needs `--yes` requires a server-enforced
`confirm: true` argument (the MCP mirror of `--yes`). `blumkin mcp install` is a
guided, idempotent registrar for the three clients (`claude mcp add` /
`copilot mcp add` where available, a JSON config merge for Cursor and for Copilot
project scope); `blumkin mcp status` shows the
result. Superseded reasoning for the old "no MCP server in v1" stance:
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

### D9 - `calendar update --no-teams` removes the online meeting on both providers

`calendar update` ([#172](https://github.com/the-hcma/blumkin/issues/172)) makes
`--teams` tri-state: omit to leave the online meeting alone, `--teams` to
attach, `--no-teams` to remove.

- **Google**: `--no-teams` sends `conferenceData: null` with
  `conferenceDataVersion=1`, which the Calendar API documents as the removal
  path. Exercised end to end by the `live_google` update test.
- **Microsoft**: `--no-teams` PATCHes `isOnlineMeeting: false`. Graph documents
  this as clearing the meeting (`onlineMeeting` goes null on the next read).
  This is **only offline-mocked** so far (the hermetic test pins the PATCH body);
  a `BLUMKIN_LIVE=1` check against a solo hold that we then delete is a
  **TODO (validate)** - see `RETROSPECTIVE-M1.md`. If a live check shows Graph
  ignores the flag, scope the removal claim to Google and document Microsoft
  `--no-teams` as attach-only.

### D10 - `docs create` scope and Markdown-subset choices ([#194](https://github.com/the-hcma/blumkin/issues/194))

`docs create` authors a document from one authoring format (a Markdown subset)
into a provider-neutral block model, rendered as a native Google Doc or an
uploaded `.docx`. Standing calls for phases 1-2:

- **Markdown subset, not a JSON block model.** Headings, bold / italic / inline
  code / links, bullet + numbered lists (one nesting level), fenced code,
  horizontal rules, and pipe tables. Anything outside the subset renders as
  plain text - never an error. Friendlier for agents and humans than a block
  DSL; the subset is small enough that both backends render it faithfully.
- **Tables render as a monospace text grid in v1.** Native Google Docs tables
  need fragile `insertTable` index arithmetic that CI (offline) cannot verify;
  `python-docx` tables are easy but asymmetry with Google is worse than a
  consistent fallback. Native tables are phase 4 ("renderer hardening").
- **Google: `documents` + `drive.file`, not `drive`.** `drive.file` only ever
  sees files blumkin created, so `--folder` targets (and reuses, or creates) a
  folder blumkin itself made - it cannot file a doc under an arbitrary existing
  Drive folder. Broad `drive` is not worth it for a personal CLI.
- **Microsoft (phase 2): a dedicated `docs_scopes` toggle for `Files.ReadWrite`,
  not a widened `files_scopes`.** `files_scopes` currently unlocks only chat-file
  *downloads* (`Files.Read`); silently widening it to write would change the
  grant for existing configs and break their MSAL silent refresh until
  re-consent. Same pattern as `wo1162425_scopes`.
- **`docs export` is deferred** to a fast-follow issue (phase 3).

### D11 - `drive` skill area: scope and provider-asymmetry choices ([#208](https://github.com/the-hcma/blumkin/issues/208) / [#212](https://github.com/the-hcma/blumkin/issues/212))

The `drive` area is the read side (`list` / `get` / `download` / `export` /
`read`, #208) and the organize side (`mkdir` / `move` / `rename`, #212). One
provider module per backend (`providers/google/drive.py`,
`providers/microsoft_drive.py`), shared shape in `skills/drive.py`.

- **No new config toggle.** #194's `docs create` established one gated broad
  scope per provider; `drive` reuses it rather than adding a third knob.
  - **Google: the full `drive` scope**, folded into `GOOGLE_SCOPES` /
    `GOOGLE_REQUIRED_SCOPES`. `drive.file` (D10) only ever sees blumkin's own
    files, and `drive.readonly` cannot rewrite `parents` (needed by #212), so
    one scope covers the whole area. Adding it re-prompts consent on the next
    `blumkin auth login` (standard Google behaviour for any added scope).
    Supersedes D10's "broad `drive` is not worth it" for this area - reading a
    Doc the user shares and filing next to it *is* the point here.
  - **Microsoft: reuse the `docs_scopes` toggle** (`Files.ReadWrite`). Every
    `drive.*` skill - read and write - is gated on it in the dispatch layer,
    mirroring `docs.create`: toggle off is a `ScopeAddonDisabledError` ->
    `usage_error` / **exit 2** (like `wo1162425_scopes`), not the `exit 4`
    `missing_scope` reserved for a genuinely ungranted OAuth scope (e.g. the
    Google `drive` scope after upgrading). No new toggle, no new consent beyond
    what `docs create` already needs.
- **`drive read` (Google Doc -> Markdown text) is Google-only.** `documents.get`
  gives a structured body to flatten (the inverse of `docs create`); Graph has
  no Word content API, so Microsoft `drive read` raises a clear
  `not supported for provider=microsoft` (same pattern as Meet transcription,
  D8). `drive export` on Microsoft is PDF-only (Graph limitation).
- **`drive export` absorbs the deferred `docs export`** (D10 / #194 OQ5). One
  `drive export`, no `docs`-specific variant.
- **All three write verbs (`mkdir` / `move` / `rename`) require `--yes` /
  `confirm`** even though none notifies anyone - a mis-aimed reparent is
  annoying to undo, and the friction is cheap. They are hidden from
  `blumkin mcp serve --read-only`.
- **`docs create --folder` targets pre-existing folders** once the `drive` scope
  is present (path resolve + create-then-move), not just blumkin-made top-level
  folders. `docs update --folder` is #211.
