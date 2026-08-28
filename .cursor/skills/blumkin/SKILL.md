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
     Freebusy `--json` items include `timezone` and `working_hours` when Graph returns
     them (from the attendee's mailbox settings via getSchedule — no extra scope).
     Before `calendar create` with a cross-zone or external attendee, run freebusy first,
     read their `timezone` / `working_hours`, and prefer overlap with *their* business day
     (roughly 09:00-17:00 local) unless the user overrides. Do **not** rewrite `--start` /
     `--end` into the attendee's zone — organizer intent stays explicit; convert mentally
     when proposing slots.
     Freebusy returns **busy blocks**, not suggested starts. Include the organizer's
     and attendees' busy blocks (pass the organizer email in `--with` too if needed),
     take the **union** of those intervals, then pick gaps in that complement. Clip to
     the organizer `--tz` / `default_tz` and each attendee's `working_hours` when
     present, and treat `tentative` as busy unless the user says otherwise. Do **not**
     use freebusy as a people directory (guessing SMTP addresses via `--with`).
   - Mail: `blumkin mail inbox --top 10 --json`
     `blumkin mail list --folder sentitems --top 20 --json` (also `archive`,
     `deleteditems`, `drafts`, `junkemail`, `outbox`, a folder id, or a custom
     folder's display name; Sent Items orders by `sentDateTime` and Drafts/Outbox by
     `createdDateTime`, since `receivedDateTime` is null there — `--orderby
     created|received|sent` overrides)
     Filters on both `mail inbox` and `mail list`: `--from` (sender name or address
     substring), `--subject`, `--unread`, `--since` / `--until` (half-open
     `[since, until)`, in `--tz` or the config default, bounding whichever date field
     the listing sorts by). Prefer these over fetching a large `--top` and filtering
     client-side.
     `--from` / `--subject` are matched **locally** over a newest-first scan, because
     Graph rejects a substring filter combined with a sort. The scan stops at 500
     messages; when it does, the payload carries `"complete": false` with `"scanned"`,
     and the human output says so. Treat an empty result as "not in the recent N",
     not "does not exist". On `--search` or a plain/filter-only listing,
     `"scanned"` / `"complete"` stay null — those fields only describe the local scan.
     `--search '<term>'` is Graph's `$search`, runs server-side across the whole
     mailbox, and **cannot** be combined with those filters or `--orderby` — Graph
     rejects both combinations, so matches come back ranked by relevance with
     `"orderby": null`.
     `blumkin mail get --id '<message-id>' --json` (one message in full: participants,
     timestamps, attachments, and body — use this instead of listing and filtering
     client-side; `--body-type html` keeps the markup, default `text`)
     `blumkin mail folders --json` (folder ids and counts, for custom folders;
     Graph's totals can lag — do not treat `total: 0` as proof a folder is empty;
     use `mail list --folder drafts` or `mail get --id` for existence)
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
     `--to` / `--cc` / `--bcc` are repeatable or comma-separated for multiple recipients.
     Add files with `--attach <path>`, repeated once per file. Each file goes up in a
     single request, so keep them under 2 MB; larger ones are refused (exit 2) rather
     than silently truncated. A bad path fails before the draft is created. If any
     upload fails after the draft exists, the draft is deleted so a retry is a no-op.
     Outlook-safe HTML: prefer simple structure (`<p>`, `<a>`, lists, tables). Inline
     `style=` and decorative borders are often stripped on send; links and headings
     usually survive. Blumkin does not sanitize markup — it passes `--body-type html`
     through unchanged.
  - `blumkin mail reply --id '<message-id>' --body …` (`--all` for reply-all). Use this
    rather than a fresh draft with `RE:` prepended: Graph puts the draft in the original
    conversation and inherits the recipients, so it threads in the recipient's client.
    Draft only — send with `mail send-draft --yes`. The draft body is HTML because it
    contains the quoted original, whatever `--body-type` you pass for your own text.
    Prefer including `--body` here: an empty reply draft filled later with
    `mail update-draft --body` *replaces* that HTML and drops the quoted original.
    To set or replace CC/BCC (or change To) after create, use `mail update-draft`
    with `--cc`, `--bcc`, or `--to` respectively. Each provided option replaces that
    entire list, so include existing recipients that must remain. Reply/forward do not
    take recipient options directly yet.
  - `blumkin mail forward --id '<message-id>' --to … --body …` (draft only; same
    update-draft warning as reply — pass `--body` on create when you can; set `--cc` /
    `--bcc` via `mail update-draft` when needed, with full list replacement semantics)
  - `blumkin mail update-draft --id '<draft-id>' --body …` (no `--yes`; `--to` / `--cc` /
    `--bcc` each replace that whole list when provided; `--body` replaces the whole body)
    `--attach <path>` works here too and *adds* to whatever the draft already carries —
    it never replaces. It is also valid on its own, without any other field.
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
   `blumkin auth login` on this machine, then retry. Do **not** treat every
   non-zero auth-adjacent exit as login: exit `1` / `secret_write_failed` means
   the token cache or auth record could not be written (often a symlink at
   `~/.config/blumkin/` or those files) — fix the path, do not re-login in a
   loop.
7. Writes that email or invite others require `--yes`.
8. Chat write + meeting transcription need `Chat.ReadWrite` /
   `OnlineMeetings.ReadWrite` consented (re-login after Identity grant).
9. Teams chat files live in SharePoint/OneDrive, so
   `blumkin chat attachments download` needs a delegated `Files.Read` scope,
   gated behind `files_scopes` (off by default). Without it, listing still works
   and download exits `4` / `missing_scope` with the share URL — hand that URL to
   the user to open in a browser instead of retrying. Do **not** spin up a second
   Graph client in the agent session to paper over that.

## Authoring style (mail + chat bodies)

When composing text for `mail draft`, `mail update-draft`, `mail reply`,
`mail forward`, or `chat send`:

- Use ASCII hyphens (`-`), not em dashes (`—`) or en dashes (`–`). Prefer two
  short sentences over a dash at all.
- Same rule for `--body-type html`: do not emit `&mdash;` / `&ndash;` (or the
  literal Unicode dashes) in markup you write on the user's behalf.
- Do **not** invent a mail signature block (colored name, title, affiliation HTML).
  If the user wants a signature, ask them to put it in the body or wait for
  config-backed signatures — do not invent branding markup per draft.

## Config

- Default: `~/.config/blumkin/` (`config.toml`, token cache, auth record).
- Override with `BLUMKIN_CONFIG_DIR`. Never invent or commit secrets.
- Keep that directory a real local folder (not a symlink into a shared tree). Token
  cache and auth-record writes refuse symlinked secret paths and report
  `secret_write_failed` (exit `1`) instead of looping on `auth login`.
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
