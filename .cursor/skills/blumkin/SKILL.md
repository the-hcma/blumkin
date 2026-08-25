---
name: blumkin
description: >-
  Personal Microsoft 365 via the blumkin CLI (calendar, mail, Teams chat,
  free/busy). Use when the user asks about their Outlook/Teams as themselves.
---
# Blumkin

1. Prefer shelling to `blumkin` over writing Graph/SDK code.
2. Run `blumkin skills list --json` if unsure which command exists.
3. Always use `--json` for machine parsing.
4. Writes that email or invite others require `--yes` (when those skills exist).
5. Auth errors → tell the user to run `blumkin auth login` on this machine.
6. Config and token cache live under `~/.config/blumkin/` (never invent client IDs).
