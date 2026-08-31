---
name: blumkin
description: >-
  Personal Microsoft 365 via the blumkin CLI (calendar, mail, Teams chat,
  free/busy). Use when the user asks about their Outlook/Teams as themselves.
---
# Blumkin

Prefer shelling to **`blumkin` on `PATH`** over writing Microsoft Graph / Azure
SDK code (or Google API client code). Do not invent client IDs or call Graph /
Workspace APIs directly when Blumkin covers the job.

With `provider = "google"` in config, supported verbs are calendar
(`today` / `view` / `freebusy` / `suggest` / `create`) and mail
(`inbox` / `list` / `get`) plus auth. Point `google_oauth_client_file` at the
Desktop client JSON (secret stays in that file, not env/toml). Setup walkthrough:
[`docs/google-setup.md`](../../../docs/google-setup.md). Unsupported verbs (chat,
people, mail writes, calendar `update` / `cancel` / `accept`, …) fail closed with
a clear error — do not invent workarounds. On Google, `calendar create` ignores
`--teams` (no Meet link yet) and `--remind-email` adds a real email reminder.

## Cold start (agent)

1. Confirm the binary exists: `blumkin --version` (if missing, tell the user to
   run `uv tool install -e .` from their blumkin clone and ensure `~/.local/bin`
   is on `PATH`).
2. Discover account profiles: `blumkin profiles list --json`.
   - If `count` is `0`, tell the user to configure the active config directory
     (`$BLUMKIN_CONFIG_DIR/config.toml` when set, else
     `~/.config/blumkin/config.toml`) and **stop** before mail, calendar, or
     chat commands.
   - If `count` is greater than 1, do **not** guess — pass `--profile <name-or-tag>`
     on every command, honor a user-chosen tag for this session, or **ask which
     account** before any mail/calendar/chat read or write. Request tags like
     `@work` / `@personal` (or wording such as “on Google” / “on Microsoft”) map
     to profile `tags` in that JSON. `--profile` wins over `BLUMKIN_PROFILE`.
3. Discover skills: `blumkin skills list --json`.
   Every `--json` payload on stdout has a top-level `ok` boolean: `true` on
   success, `false` on a fail-closed stdout result (`doctor`, `chat last` with
   no match, `people resolve` ambiguous). Errors print `{... "ok": false}` on
   stderr. Branch on `ok`, then on exit code / `error`.
4. Reads (prefer `--json`):
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
     Freebusy returns **busy blocks**, not suggested starts. Prefer
     `blumkin calendar suggest --with … --start … --end … --duration 45m --json`
     (optional `--window 09:00-18:00`, `--treat-tentative busy|free`) to get ranked
     mutual-free starts from the union of busy intervals. Include the organizer in
     `--with` when they must be free too. Clip with `--window` / organizer `--tz`;
     do **not** rewrite times into an attendee's zone. Do **not** use freebusy as a
     people directory (guessing SMTP addresses via `--with`). Resolve names first
     with `blumkin people resolve --name "Display Name" --json` (or `--email`).
   - People: `blumkin people resolve --name "Display Name" --json`
     (optional `--email` for reverse / exact filter). Requires
     `wo1162425_scopes` + Graph `People.Read` (on the augmented WO1162425 ask;
     **not granted yet** as of 2026-08 — leave the flag off until Identity
     finishes, then wipe cache and `auth login`). Uses Graph `/me/people`.
     On **exactly one** match, `person.email` is the address to use. On **zero**
     matches: exit `5` / `not_found`. On **multiple** matches: stdout carries
     `ambiguous: true` and the candidate list, exit `2` / `usage_error` —
     **ask the user which person** (or demand an exact email); never pick a
     winner and never compose/invite until confirmed.
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
5. Writes (require `--yes` when they notify others):
   - `blumkin calendar accept --event-id '<id>' --yes`
   - `blumkin calendar create --subject … --start … --yes`
     (Teams online meeting by default; pass `--no-teams` for an offline hold.
     `--with email` is optional - omit it for a solo hold that notifies nobody;
     `--yes` is still required. `--remind-email 30m|1h|1d|1w` adds a reminder:
     a real email on Google, an Outlook popup on Microsoft.)
   - `blumkin calendar update --event-id '<id>' --yes` (attach Teams to an
     existing event; uses Calendars.ReadWrite, not OnlineMeetings.ReadWrite)
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
    Prefer `--cc` / `--bcc` on create when adding people (merged into Graph-inherited
    recipients). Use `mail update-draft --cc` / `--bcc` / `--to` only when you must
    *replace* an entire list — include every address that should remain.
  - `blumkin mail forward --id '<message-id>' --to … --body …` (draft only; same
    body/`update-draft` warning as reply — pass `--body` on create when you can;
    `--cc` / `--bcc` on create merge; `update-draft` replaces wholesale)
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
6. TZ: `blumkin --tz AREA …` or per calendar command `--tz AREA` (omit for config default).
7. On auth failure (exit `3` / `auth_required`): tell the user to run
   `blumkin auth login` on this machine, then retry. Do **not** treat every
   non-zero auth-adjacent exit as login: exit `1` / `secret_write_failed` means
   the token cache or auth record could not be written (often a symlink at
   `~/.config/blumkin/` or those files) — fix the path, do not re-login in a
   loop. Exit `1` / `timeout` means Graph or token HTTP exceeded
   `graph_timeout_seconds` in `config.toml` (default 60).
   Agent shells should set `BLUMKIN_NONINTERACTIVE=1` so Blumkin never opens a
   browser. If a command hangs: `pkill -f blumkin`, check
   `blumkin auth status --json` for `access_token_expired`, then
   `blumkin auth refresh` (or `auth login` on a TTY) before retrying mail/calendar.
8. Writes that email or invite others require `--yes`.
9. Chat write + meeting transcription need `Chat.ReadWrite` /
   `OnlineMeetings.ReadWrite` consented (re-login after Identity grant).
10. Teams chat files live in SharePoint/OneDrive, so
   `blumkin chat attachments download` needs a delegated `Files.Read` scope,
   gated behind `files_scopes` (off by default). Without it, listing still works
   and download exits `4` / `missing_scope` with the share URL — hand that URL to
   the user to open in Teams/browser (or save into a local drop folder) instead of
   retrying. Do **not** spin up a second Graph client in the agent session to paper
   over that. Listing must not `$expand=attachments` (Graph 400).

## Authoring style (mail + chat bodies)

When composing text for `mail draft`, `mail update-draft`, `mail reply`,
`mail forward`, or `chat send`:

- Use ASCII hyphens (`-`), not em dashes (`—`) or en dashes (`–`). Prefer two
  short sentences over a dash at all.
- Same rule for `--body-type html`: do not emit `&mdash;` / `&ndash;` (or the
  literal Unicode dashes) in markup you write on the user's behalf.
- Prefer the configured mail signature (below) over inventing colored name/title
  HTML per draft. Use `--no-signature` when the body already includes one.

## Config

- Default: `~/.config/blumkin/config.toml`. Named profiles live under
  `[profiles.<name>]`; token files under `profiles/<name>/`. Legacy flat toml
  (no `[profiles.*]`) is one implicit profile `default` with tokens in the
  config dir root.
- Select with `--profile <name-or-tag>` or `BLUMKIN_PROFILE` (non-secret). 
  `BLUMKIN_CONFIG_DIR` still selects the config **directory** only. Never invent
  or commit secrets; no credential env overrides.
- Keep that directory a real local folder (not a symlink into a shared tree). Token
  cache and auth-record writes refuse symlinked secret paths and report
  `secret_write_failed` (exit `1`) instead of looping on `auth login`.
- **Mail signature (optional):** under the profile, e.g. `[profiles.work.mail.signature]`
  (legacy: `[mail.signature]`):

  ```toml
  [profiles.work.mail.signature]
  enabled = true
  name = "Ada Example"
  affiliation = "Example Org"
  title = "Example Title"
  name_color = "#003366"
  title_color = "#5B9BD5"
  # optional: html_template = "<p>…</p>"  # replaces the default HTML layout
  ```

  When `enabled = true`, `mail draft`, `mail reply`, and `mail forward` append the
  rendered signature (HTML or plain text matching `--body-type`). Pass
  `--no-signature` to skip. Do not invent signature markup in the agent session.
- **WO1162425 add-on scopes (off by default):** `wo1162425_scopes = true` in
  `config.toml` after Remedy WO1162425 grants its add-ons (runtime requests
  `Chat.ReadWrite`, `OnlineMeetings.ReadWrite`, `People.Read`; full augmented ask
  list in `HANDOFF.md` — fulfillment may still be pending). Then delete token
  cache + auth record and `blumkin auth login`. While off, calendar/mail/chat
  **read** skills use the base scope set; chat write, meeting commands, and
  `people resolve` refuse with `usage_error`.
- **Files scope for chat downloads (off by default):** `files_scopes = true` in
  `config.toml` once the tenant grants `Files.Read`. Then delete token cache +
  auth record and `blumkin auth login`. While off, `chat attachments` listing
  works but `chat attachments download` exits `4` / `missing_scope` with the
  share URL.
