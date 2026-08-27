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
     `blumkin mail list --folder sentitems --top 20 --json` (also `archive`,
     `deleteditems`, `drafts`, `junkemail`, `outbox`, or a raw folder id; sent-style
     folders order by `sentDateTime` since `receivedDateTime` is null there)
     `blumkin mail folders --json` (folder ids and counts, for custom folders)
     `blumkin mail attachments --id '<message-id>' --json`
     `blumkin mail attachments download --message-id '<message-id>' --attachment-id '<id>' --out ./file.docx`
   - Chat: `blumkin chat find --with "Name" --json`,
     `blumkin chat last --with "Name" --n 3 --json`
     `blumkin chat attachments --chat-id '<chat-id>' --message-id '<message-id>' --json`
     `blumkin chat attachments --with "Name" --latest --json` (newest message carrying files)
     `blumkin chat attachments download --chat-id '<chat-id>' --message-id '<message-id>' --attachment-id '<id>' --out ./file.docx`
     `blumkin chat attachments download --with "Name" --latest --all --out ./downloads/`
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
9. Teams chat files live in SharePoint/OneDrive, so
   `blumkin chat attachments download` needs a delegated `Files.Read` scope,
   gated behind `files_scopes` (off by default). Without it, listing still works
   and download exits `4` / `missing_scope` with the share URL — hand that URL to
   the user to open in a browser instead of retrying.

## Config

- Default: `~/.config/blumkin/` (`config.toml`, token cache, auth record).
- Override with `BLUMKIN_CONFIG_DIR`. Never invent or commit secrets.
- **WO1162425 add-on scopes (off by default):** `wo1162425_scopes = true` in
  `config.toml` or `BLUMKIN_WO1162425_SCOPES=1` after Remedy WO1162425 grants
  `Chat.ReadWrite` + `OnlineMeetings.ReadWrite`. Then delete token cache + auth
  record and `blumkin auth login`. While off, calendar/mail/chat **read** skills
  use the base scope set; chat write + meeting commands refuse with `usage_error`.
- **Files scope for chat downloads (off by default):** `files_scopes = true` in
  `config.toml` or `BLUMKIN_FILES_SCOPES=1` once the tenant grants `Files.Read`.
  Then delete token cache + auth record and `blumkin auth login`. While off,
  `chat attachments` listing works but `chat attachments download` exits `4` /
  `missing_scope` with the share URL.
