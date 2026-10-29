---
name: blumkin-dev
description: >-
  Run the unreleased blumkin code from this checkout instead of the installed `blumkin`. Use only when the user asks for the dev version, to run blumkin from the checkout or a worktree, or to test unreleased changes; otherwise use the `blumkin` skill.
---
# Blumkin (dev checkout)

Use this skill only when the user asks for the dev version of blumkin, to run it from the checkout (or a worktree of it), or to test changes that are not released. For everything else use the `blumkin` skill and the installed `blumkin` on `PATH`.

Run **`scripts/dev/blumkin <args>`** (from the checkout root, or by its absolute path from anywhere) wherever the `blumkin` skill says `blumkin <args>`. It runs the in-repo code with no install step. Do not run `uv run blumkin` yourself and do not reinstall to get the checkout: the launcher is the supported way.

## Before anything else

Setting up from scratch (no config, no profiles, MCP registration)? Follow [`docs/agent-onboarding.md`](../../../docs/agent-onboarding.md); this skill covers the rules once it works.

1. Run `scripts/dev/blumkin --version`. The first line must contain a dev marker: `+dev.<short sha>.<commit date>`, with `-dirty` after it when the checkout has uncommitted or untracked changes. Report that exact line to the user, so they know which code ran.
2. If the marker is missing you are not running the checkout (for example an installed release answered). Stop and tell the user instead of continuing.
3. `scripts/dev/blumkin doctor` shows the source tree path, the resolved config directory and what selected it (`BLUMKIN_CONFIG_DIR`, `XDG_CONFIG_HOME` or the default). Use it to confirm which state you are about to touch.

## Same state, same rules

The dev run uses the **real** config directory, keychain entries, token cache and agent state, with no separate namespace. Every rule of the `blumkin` skill applies unchanged: no-secret-exposure, no-third-party-side-effects (verify with reads or a draft you delete, never with a send, invite or chat), the authoring style for mail and chat text, and a fresh confirmation before any call that notifies someone. Being on unreleased code is a reason for more care, not less.

For a throwaway setup, point `BLUMKIN_CONFIG_DIR` at an empty directory for the commands you run.

## First run

If the config directory has no `config.toml`, the launcher offers the guided `auth setup` on a terminal and fails with a hint without one. It never creates or overwrites anything unless the user says yes. Without a terminal, `scripts/dev/blumkin auth setup --yes --provider google|microsoft` plus the value flags (see `auth setup --help`) creates the minimal config (directory mode 0700, file mode 0600) and fills in the OAuth client. Do not paste secrets into command arguments; use the flags and prompts the command documents. Only provider-bound commands, and the ones that create state under the config directory, need a config (`mail`, `calendar`, `chat`, `drive`, `docs`, `people`, `meeting`, `tasks`, `capabilities`, `doctor`, `auth` apart from `auth setup`, `mcp serve`, `browser`, `loop` and `receipts`). The rest run without one: `profiles list` answers `count: 0`, which is the cold-start check the `blumkin` skill relies on, and `completion`, `agent` and `mcp install` need no credentials.

If `scripts/dev` is on `PATH`, a bare `blumkin` runs that launcher, and so the code of whichever checkout it belongs to, not the installed release. From a worktree it can even be the primary checkout's code. Check `blumkin --version` for the `+dev` marker before trusting what a bare `blumkin` did.

## MCP

`scripts/dev/blumkin mcp install` registers the launcher itself, so the MCP client runs the checkout with the dev marker. To register by hand, set the client's `command` to the absolute path of `scripts/dev/blumkin` and its `args` to `["mcp", "serve"]`. The MCP server's version carries the same dev marker, and it asks to be restarted when the checkout changes under it (the marker includes `-dirty`).
