---
description: Choose Graphite vs GitHub Stacked PRs from .github/stacking-tool
alwaysApply: true
---

# Stacking tool preference

Before creating branches or submitting/restacking PRs, **read** `.github/stacking-tool`
(single line: `graphite` or `gh-stack`). Missing or invalid values: **stop and ask** —
do not guess.

<!-- stacking-tool-canonical-graphite-skill: https://github.com/the-hcma/repository-helpers/blob/main/.agents/skills/graphite/SKILL.md -->
<!-- stacking-tool-canonical-gh-stack-skill: https://github.com/the-hcma/repository-helpers/blob/main/.agents/skills/gh-stack/SKILL.md -->

Canonical skills live in **repository-helpers** (not copied into this repo):

- **Graphite:** https://github.com/the-hcma/repository-helpers/blob/main/.agents/skills/graphite/SKILL.md
- **gh-stack:** https://github.com/the-hcma/repository-helpers/blob/main/.agents/skills/gh-stack/SKILL.md

Resolve the helpers clone once:

```bash
rh="${REPOSITORY_HELPERS_DIR:-$HOME/work/ai/repository-helpers}"
```

Local clone (when `${rh}` is synced):

- `${rh}/.agents/skills/graphite/SKILL.md`
- `${rh}/.agents/skills/gh-stack/SKILL.md`

## `graphite`

- Follow the Graphite skill above (`gt create` / `gt submit` / `gt restack`).
- After `"${rh}/scripts/dev/pre-pr-checks"`, submit with bare
  `gt submit --publish --no-interactive` from this repo's worktree.
  Do **not** run `"${rh}/scripts/dev/submit-stack"` from a consumer (it targets
  the helpers clone).

## `gh-stack`

- Follow the gh-stack skill (non-interactive: `view --json`, `submit --auto --open`,
  named `init`/`add`).
- After `"${rh}/scripts/dev/pre-pr-checks"`, submit with bare
  `gh stack submit --auto --open` from this repo's worktree.
  Do **not** run `"${rh}/scripts/dev/submit-stack"` from a consumer (it targets
  the helpers clone). Use `"${rh}/scripts/wait-for-agent-review"` /
  `"${rh}/scripts/dev/post-pr-submission-checks"` for CI wait and review.
- **Do not** mix with `gt create` / `gt submit` / `gt restack` on the same stack.

## Marker cutover checklist

When flipping `.github/stacking-tool` (or landing an MQ/`gh-stack` cutover PR):

1. Update `AGENTS.md` stacking/merge guidance to match the new marker (and GitHub
   auto-merge: `gh pr merge --auto --squash` — not `merge-it`).
2. Rewrite `.agents/rules/pr-ship-and-review.md` submit block to the marker-aware
   template in `scripts/lib/repo-practices-agents/rules/pr-ship-and-review.md`
   (or ensure it documents both backends gated on the marker; keep a thin
   `.cursor/rules/pr-ship-and-review.mdc` shim).
3. Delete root `GRAPHITE.md` when switching to `gh-stack` (canonical skill lives in
   repository-helpers).
4. Keep `.agents/rules/stacking-tool.md` (+ Cursor shim) in sync with the consumer
   template.
5. Re-run `"${rh}/scripts/github-repo-lint" --repo OWNER/NAME --suggest --strict-onboarding`
   and fix stacking-docs consistency findings.

## Unchanged regardless of marker

Agent review, CI wait, and reply-before-resolve still follow
`.agents/rules/pr-ship-and-review.md` and the canonical ship-and-review skill in
repository-helpers.
