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
(`today` / `view` / `freebusy` / `suggest` / `create` / `get` / `list`), mail
(`inbox` / `list` / `get` / `search` / `thread` / `folders` / `attachments` / `attachments download`),
mail writes
(`draft` / `update-draft` / `delete-draft` / `send-draft` / `reply` / `forward` /
`move` / `mark` / `delete` - the last three need the `gmail.modify` scope),
calendar writes (`accept` / `decline` / `tentative` / `cancel` / `update`;
`--propose-time` is Microsoft-only), `people resolve`, and chat
(`find` / `last` / `send` / `edit` / `delete` / `attachments`) plus auth. Point `google_oauth_client_file` at the Desktop client JSON (secret
stays in that file, not env/toml). Setup walkthrough:
[`docs/google-setup.md`](../../../docs/google-setup.md). Unsupported verbs (meeting, …) fail closed
with a clear error — do not invent workarounds. `mail folders` lists Gmail
labels that act as folders (system labels map to Outlook-style names; user
labels keep their `Parent/Child` path); `mail list --folder` still only accepts
the well-known names. On Google, `calendar create`
ignores `--teams` (use `calendar update` to attach a Meet link) and `--remind-email` adds a real email
reminder. Mail writes need the `gmail.compose` scope: after upgrading, run
`blumkin auth login` once to re-consent, or those calls exit 4 (`missing_scope`).
The returned draft `id` is the Gmail draft id (pass it straight back to
`send-draft` / `delete-draft`); `attachments[].id` is `null` because Gmail
carries attachments inside the raw message. `reply` threads with the original
(sets `threadId` + `In-Reply-To`); `forward` starts a new thread and carries the
original's attachments.

## Cold start (agent)

1. Confirm the binary exists: `blumkin --version` (if missing, tell the user to
   run `pipx install blumkin && pipx ensurepath`, or `uv tool install -e .` from
   a clone for dev). `--version` also prints the commit and resolved path — use
   it when behavior does not match this skill.
2. Discover account profiles: `blumkin profiles list --json` (each entry carries
   `email`, the address recorded at first login, when it is known).
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
     (half-open `[from,to)`),
     `blumkin calendar get --event-id '<id>' --json` (one event in full: body,
     every attendee + their response, recurrence, join URL, `series_master_id`),
     `blumkin calendar list --json` (every calendar the account can see: `id`,
     `name`, `is_default`, `can_edit`, `owner`). Pass a `name` or `id` from it to
     `--calendar` on `today` / `view` / `get` / `create` / `update` / `cancel` to
     target a non-default calendar (ambiguous name -> usage error; use the id).
     `blumkin calendar freebusy --with email --start … --end … --json`
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
     people directory (guessing SMTP addresses via `--with`). Resolve names first.
   - People (names -> address): blumkin never turns a name into an address for
     you; `--to` / `--cc` / `--with` are email-only.
     1. `blumkin people context --json` — the operator's `email-context.md`
        (name, aliases, email, notes). Fuzzy-match the user's phrasing against
        it yourself. An entry with `conflict: true` has clashing copies across
        files — ask the user, do not pick one.
     2. If nothing fits, `blumkin people resolve --name "Display Name" --json`
        (Graph directory, Microsoft only, needs `wo1162425_scopes`).
     3. **Confirm the concrete recipient(s) with the user** ("draft to Sam —
        sam@example.com?") before you compose or invite.
     4. Call `mail draft` / `calendar create` with the real address.
   - People (Graph search): `blumkin people resolve --name "Display Name" --json`
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
     `blumkin mail search --query '<term>' --json` searches the WHOLE mailbox
     (every folder), relevance-ranked, tagging each hit with its `folder` —
     prefer this over `mail list --search` (one folder). Google `--query` takes
     Gmail operators (`from:` / `subject:` / `has:attachment`). `--since` /
     `--until` filter the returned page locally.
     `blumkin mail thread --id '<message-id>' --json` lists every message in that
     conversation, oldest first; `--full` adds each body.
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
   - Task templates (recurring jobs): `blumkin tasks list --json`. Match the
     user's request to a template's `trigger`, **confirm the pick with the
     user**, then `blumkin tasks show --name <it> --json` and run the returned
     `prompt` yourself against the input it implies (fetch that input with the
     `mail` / `drive` reads above). blumkin picks nothing and runs no model. A
     template with `conflict: true` has clashing copies — ask the user.
5. Writes (require `--yes` when they notify others):
   - `blumkin calendar accept --event-id '<id>' [--comment TEXT] --yes`
   - `blumkin calendar decline --event-id '<id>' [--comment TEXT] --yes` /
     `blumkin calendar tentative …` - RSVP no / maybe. Both take
     `--today-pending` (batch every unanswered invite for today, reporting
     skips) like `calendar accept`. `--propose-time <start> [--propose-duration]`
     suggests another slot **on Microsoft only** (fails closed on Google) and
     needs a single `--event-id`.
   - `blumkin calendar create --subject … --start … --yes`
     (Teams online meeting by default; pass `--no-teams` for an offline hold.
     `--with email` is optional - omit it for a solo hold that notifies nobody;
     `--yes` is still required. `--remind-email 30m|1h|1d|1w` adds a reminder:
     a real email on Google, an Outlook popup on Microsoft.
     `--repeat daily|weekly|monthly` makes a recurring series; bound it with
     `--until YYYY-MM-DD` or `--count N` (omit both for an open-ended series),
     `--interval N` widens the gap, and `--days mon,tue,...` restricts a weekly
     pattern (must include the `--start` weekday).
     `--body`/`--body-file` set an agenda, `--location` is free text,
     `--optional email` adds an optional attendee, and `--all-day` makes
     `--start` a date with `--duration` in whole days (no Teams link; a
     date-only `--start` without `--all-day` is rejected).)
   - `blumkin calendar update --event-id '<id>' [--subject …] [--start …]
     [--duration …|--end …] [--location …] [--body …] [--with … (replaces the
     attendee list)] [--teams|--no-teams (attach/remove online meeting)]
     [--all-day|--no-all-day] --yes` - only the flags you pass change; editing a
     recurring series edits the whole series
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
  - `blumkin mail move --id '<id>' --to archive --yes` / `mail mark --id '<id>'
    [--read/--unread] [--flag/--unflag] [--importance high|normal|low] --yes` /
    `mail delete --id '<id>' --yes` - triage. `--id` is repeatable; a batch
    reports the messages it skipped. `delete` is to Deleted Items / Trash
    (recoverable). `--yes` is a safety confirm (nobody is notified).
    Microsoft: `Mail.ReadWrite` (already granted). **Google: needs the new
    `gmail.modify` scope** - until `blumkin auth login` re-consents, these exit 4
    (`missing_scope`). On Gmail, `--flag` = the STARRED label, `--importance` =
    IMPORTANT, `--to archive` removes the Inbox label. `--to` also takes a folder
    display name (Microsoft matches your folder tree; Google resolves a Gmail
    label name to its id) or a raw folder / `Label_` id from `mail folders`.
  - `blumkin mail auto-reply` (alias `mail oof`) - read, set, or clear the
    automatic-reply / vacation responder. No flags = read. `--on --message '…'
    --yes` turns it on (`--message-file` reads a file instead); `--off --yes`
    clears it. `--start` / `--until` (YYYY-MM-DD) schedule a window. `--external
    all|contacts|none` picks who outside your org gets a reply. Microsoft splits
    internal/external bodies (`--external-message`) and needs `wo1162425_scopes`
    (MailboxSettings.ReadWrite); **Google has one body and needs the new
    `gmail.settings.basic` scope** - re-run `blumkin auth login` or it exits 4.
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
   `graph_timeout_seconds` in `config.toml` (default 60). Exit `1` /
   `transient_error` means a network/server hiccup talking to the auth
   provider, not a bad grant — safe to retry the same command once.
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
  `[profiles.<name>]`; token files under `profiles/<name>/`. Flat top-level
  keys (no `[profiles.*]`) are not a valid layout - wrap them in
  `[profiles.<name>]`.
- Select with `--profile <name-or-tag>` or `BLUMKIN_PROFILE` (non-secret). 
  `BLUMKIN_CONFIG_DIR` still selects the config **directory** only. Never invent
  or commit secrets; no credential env overrides.
- Keep that directory a real local folder (not a symlink into a shared tree). Token
  cache and auth-record writes refuse symlinked secret paths and report
  `secret_write_failed` (exit `1`) instead of looping on `auth login`.
- **Preferences (optional):** `font_name`, `font_size`, `html_email` (defaults to
  `true`) live in a top-level `[preferences]` table (applies to every profile) and/or
  a per-profile `[profiles.<name>.preferences]` override:

  ```toml
  [preferences]
  font_name = "Calibri"
  font_size = 11
  html_email = true

  [profiles.personal.preferences]
  font_size = 13  # overrides just this key for this profile
  ```

  A profile value that disagrees with the top-level one for the same key still
  wins, but blumkin warns on stderr (`warning: profile '<name>' sets
  preferences.<key> = …, overriding preferences.<key> = … set at the top level`)
  since it may be drift rather than an intentional per-profile tweak.
- **Mail signature (optional):** under the profile, e.g. `[profiles.work.mail.signature]`:

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
  If `auth login` / `doctor` detect that Outlook itself auto-inserts a signature
  (Graph has no API for that setting, so blumkin probes with a throwaway draft),
  blumkin stops appending `[mail.signature]` to drafts so the message is not
  signed twice; `blumkin mail signature --json` reports `suppressed: true`. Re-run
  `blumkin doctor` after changing the Outlook setting.

  If the probe cannot run for this profile (Google, reply/forward before issue
  #231 lands, or before `auth login` has ever run), set
  `client_appends_signature = true` under `[profiles.<name>.mail.signature]` to
  suppress `[mail.signature]` manually instead. It is an unconditional
  per-profile override - do not set it unless the client genuinely appends its
  own signature; `blumkin mail signature --json` reports `suppressed: true`
  either way, but does not say which mechanism caused it.
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
