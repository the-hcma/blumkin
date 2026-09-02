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

### D2 - Personal account (`the-hcma`), single code owner

The repo lives under a personal account with @thehcma as the sole code owner.
This is a personal productivity tool, not a team service. The compensating
controls for a one-person project:

- `main` cannot be pushed to directly; every change is a PR.
- Branch protection requires a code-owner approval + `require_last_push_approval`;
  [`.github/CODEOWNERS`](../.github/CODEOWNERS) is `* @thehcma`, so **no external
  contribution merges without @thehcma's review**.
- The maintainer's own PRs still run the full agent review + required checks and
  are admin-merged only after those are clear (GitHub blocks self-approval).
- Revisit if a second regular contributor appears - at that point add them as a
  reviewer and drop the admin-merge exception.

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
