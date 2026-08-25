# AGENTS.md — Ground rules for blumkin

Non-negotiable standards for humans and AI working in this repo.

---

## What this repo is

**blumkin** is a personal Microsoft 365 / Graph **skills CLI** (delegated “as me”).
See `README.md` and `PLAN.md`.

---

## Session startup & cleanup

- At the start of every session (before implementation), run
  `~/work/ai/repository-helpers/scripts/dev/start-development` from
  [repository-helpers](https://github.com/the-hcma/repository-helpers).
- Default: prompts for a stack name and creates `.worktrees/<stack-name>-wt`.
- Non-interactive:
  ```bash
  ~/work/ai/repository-helpers/scripts/dev/start-development --worktree <stack-name> --no-interactive
  ```
- After it finishes, **`cd` into the stack worktree** before any other work.
  Do not stay on the primary clone.

### Main worktree is off-limits (agents)

The **primary clone** (first entry in `git worktree list`, usually `main`) is
**read-only** unless the user explicitly authorizes writes in this conversation.

Never on the main worktree without that authorization: edit sources, run
formatters/tests that mutate the tree, commits, checkouts, or `gh stack` writes.

Always implement in `.worktrees/<stack-name>-wt`.

---

## Language & runtime

- Target **Python 3.14+**.
- Use **`uv`** as the package manager and runner (`uv sync`, `uv run …`).
- CLI entrypoint will be `blumkin` (see `PLAN.md`). Application code is not
  present yet — follow `PLAN.md` when implementing.

---

## Formatting & linting

- **Ruff** for lint + format (`[tool.ruff]` in `pyproject.toml` once added).
  - `uv run ruff check .`
  - `uv run ruff format .` (CI: `--check`)
- **Pyright** (basic) for types: `uv run pyright`
- CI must use the combined **Python lint & format checks** job via
  `.github/ci/python-static` (repository-helpers convention) — no split
  Ruff/Pyright workflow job names.
- Shell scripts (if any): **no `.sh` extension**; **shellcheck** required;
  lowercase locals; prefer long flags; timeouts on network commands.

---

## Testing

- Tests live under `tests/`.
- Prefer `uv run pytest` (or a small `./test` wrapper once added).
- New behavior needs tests. Do not merge if the suite fails.

---

## Secrets & Graph auth

- Never commit secrets, client secrets, tokens, or `.msal_token_cache.json` /
  `.auth_record.json` / `.env`.
- Blumkin uses **delegated** Graph auth only (public client + interactive
  browser). No app-only mail/calendar permissions in this product.

---

## Commits, stacking & PRs

- Stacking backend is **`gh-stack`** (`.github/stacking-tool`). Do **not** use
  Graphite (`gt`) on this repo.
- Skill reference:
  [gh-stack](https://github.com/the-hcma/repository-helpers/blob/main/.cursor/skills/gh-stack/SKILL.md)
- Worktree-per-stack via `start-development` (above).
- Prefer `scripts/dev/submit-stack` from repository-helpers with `--auto`
  (and prefer `--open`). Never interactive `gh stack submit` / `gh stack view`
  without `--json`.
- Merge path: GitHub merge queue — `gh pr merge --auto --squash` only when the
  operator asks. **Always ask before enabling auto-merge.**
- Conventional Commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`.
- Commit identity: follow `.cursor/rules/git-commit-identity.mdc` (no
  Co-authored-by trailers unless the user asks).

---

## Repository practices / lint

- Org compliance is enforced by
  `~/work/ai/repository-helpers/scripts/github-repo-lint`.
- After changing `.github/workflows/**`, Dependabot, stacking marker, or related
  config, re-run:
  ```bash
  ~/work/ai/repository-helpers/scripts/github-repo-lint --suggest
  ```
  (or `--repo OWNER/NAME --suggest` once a GitHub remote exists).
- Onboard / fix with `--new-repo` / `--apply-fix` as documented in
  repository-helpers.
- Keep cursor rules in sync with
  `repository-helpers/scripts/lib/repo-practices-cursor/` templates when those
  templates change.

---

## Lexicographic code organization

Follow `.cursor/rules/lexicographic-code-organization.mdc` for module/file
layout (imports, symbols, sections ordered for skimmability).

---

## Pre-PR checklist

From the stack worktree:

```bash
~/work/ai/repository-helpers/scripts/dev/pre-pr-checks
# or full ship path:
~/work/ai/repository-helpers/scripts/dev/submit-stack --auto --open
```

Expect secret-scan (gitleaks) once `.github/ci/secret-scan` is adopted.
