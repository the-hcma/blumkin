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
3. Reads (prefer `--json`):
   - Calendar: `blumkin calendar today --json`,
     `blumkin calendar view --from YYYY-MM-DD --to YYYY-MM-DD --json`
     (half-open `[from,to)`), `blumkin calendar freebusy --with email --start … --end … --json`
   - Mail: `blumkin mail inbox --top 10 --json`
   - Chat: `blumkin chat find --with "Name" --json`,
     `blumkin chat last --with "Name" --n 3 --json`
4. TZ: `blumkin --tz AREA …` or per calendar command `--tz AREA` (omit for config default).
5. On auth failure (exit `3` / `auth_required`): tell the user to run
   `blumkin auth login` on this machine, then retry.
6. Writes that email or invite others require `--yes` (when those skills exist).

## Config

- Default: `~/.config/blumkin/` (`config.toml`, token cache, auth record).
- Override with `BLUMKIN_CONFIG_DIR`. Never invent or commit secrets.
