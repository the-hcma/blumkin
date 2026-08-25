---
name: blumkin
description: >-
  Personal Microsoft 365 via the blumkin CLI (calendar, mail, Teams chat,
  free/busy). Use when the user asks about their Outlook/Teams as themselves.
---
# Blumkin

Prefer shelling to **`blumkin` on `PATH`** over writing Microsoft Graph / Azure
SDK code. Do not invent client IDs or call Graph APIs directly when Blumkin
covers the job.

## Cold start (agent)

1. Confirm the binary exists: `blumkin --version` (if missing, tell the user to
   run `uv tool install -e .` from their blumkin clone and ensure `~/.local/bin`
   is on `PATH`).
2. Discover skills: `blumkin skills list --json`.
3. For calendar: `blumkin calendar today --json` (optional `--date YYYY-MM-DD`;
   global TZ as `blumkin --tz AREA calendar today --json`).
4. Always pass `--json` when parsing results in agent mode.
5. On auth failure (exit `3` / `auth_required`): tell the user to run
   `blumkin auth login` on this machine, then retry.
6. Writes that email or invite others require `--yes` (when those skills exist).

## Config

- Default: `~/.config/blumkin/` (`config.toml`, token cache, auth record).
- Override with `BLUMKIN_CONFIG_DIR`. Never invent or commit secrets.
