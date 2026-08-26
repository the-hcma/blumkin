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
4. Writes (require `--yes` when they notify others):
   - `blumkin calendar accept --event-id '<id>' --yes`
   - `blumkin calendar create --subject … --with email --start … --yes`
   - `blumkin calendar cancel --event-id '<id>' --yes`
   - `blumkin mail draft --to … --subject … --body …` (draft only; `--body-type html` / `--body-file` optional)
   - `blumkin mail update-draft --id '<draft-id>' --body …` (no `--yes`; `--to` replaces the whole To list and refuses multi-To drafts)
   - `blumkin mail delete-draft --id '<draft-id>'` (no `--yes`)
   - `blumkin mail send-draft --id '<draft-id>' --yes`
   - `blumkin chat send --with "Name" --text "…" --yes` (or `--chat-id` if ambiguous)
   - `blumkin chat edit --chat-id … --message-id … --text "…" --yes`
   - `blumkin chat delete --chat-id … --message-id … --yes`
   - `blumkin meeting get --event-id '<id>'` (organizer-only online meetings)
   - `blumkin meeting transcription --event-id '<id>'` (show flags)
   - `blumkin meeting transcription --event-id '<id>' --enable --yes`
5. TZ: `blumkin --tz AREA …` or per calendar command `--tz AREA` (omit for config default).
6. On auth failure (exit `3` / `auth_required`): tell the user to run
   `blumkin auth login` on this machine, then retry.
7. Writes that email or invite others require `--yes`.
8. Chat write + meeting transcription need `Chat.ReadWrite` /
   `OnlineMeetings.ReadWrite` consented (re-login after Identity grant).

## Config

- Default: `~/.config/blumkin/` (`config.toml`, token cache, auth record).
- Override with `BLUMKIN_CONFIG_DIR`. Never invent or commit secrets.
