"""Help and usage-example text for the blumkin CLI.

Kept out of ``cli.py`` so the command wiring stays skimmable. Every constant is
an ``epilog`` string passed to a Click group or command: it renders after the
options block. Paragraphs are rewrapped by Click unless they start with a ``\\b``
marker, so each preformatted example block is prefixed with ``\\b``.

Authoring style: ASCII hyphens only (no em/en dashes), matching
``.cursor/skills/blumkin/SKILL.md``.
"""

from __future__ import annotations

AUTH_EPILOG = """
Examples:

\b
  # First run on a machine (opens a browser once)
  blumkin auth login
\b
  # Is the cached token still good?
  blumkin auth status --json
\b
  # Renew a stale access token without a browser (agent-safe)
  blumkin auth refresh
\b
  # Forget this machine's tokens
  blumkin auth logout

Token cache and auth record live under the active config dir
($BLUMKIN_CONFIG_DIR, else ~/.config/blumkin/). Never commit them.
"""

AUTH_LOGIN_EPILOG = """
Example:

\b
  blumkin auth login

Opens the system browser for delegated (public-client) sign-in, then writes the
token cache and auth record under the active config dir. Run it once per machine,
or again after `auth logout` or a scope change. In non-interactive shells set
BLUMKIN_NONINTERACTIVE=1 and use `auth refresh` instead.

Google: if the stored grant is missing a scope this build needs, this
automatically re-opens the consent screen (a warning names the gap first) -
tick every box, or click "Select all", so the grant does not stay partial.
"""

AUTH_LOGOUT_EPILOG = """
Example:

\b
  blumkin auth logout

Deletes the local token cache and auth record. The next Graph call needs a fresh
`auth login`.
"""

AUTH_REFRESH_EPILOG = """
Example:

\b
  blumkin auth refresh

Uses the cached refresh token to mint a new access token. Never opens a browser,
so it is the safe choice inside agent sessions. Exit 3 (auth_required) means the
refresh token is gone or revoked - run `auth login` on a TTY.
"""

AUTH_STATUS_EPILOG = """
Examples:

\b
  blumkin auth status
  blumkin auth status --json

Shows the resolved config path, whether the client id is set, and whether the
token cache / auth record exist plus the access-token expiry. Read this before
assuming a hang is a login problem. `--json` also carries `granted_scopes` and
`missing_scopes`, so a scope gap is visible before a command fails on it.
"""

CALENDAR_ACCEPT_EPILOG = """
Examples:

\b
  # Accept one invitation by event id
  blumkin calendar accept --event-id AAMk... --yes
\b
  # Accept everything not yet responded to for today
  blumkin calendar accept --today-pending --yes
\b
  # Accept with a note to the organizer
  blumkin calendar accept --event-id AAMk... --comment "on it" --yes

Sends a response to each organizer, so `--yes` is required. Get event ids from
`blumkin calendar today --json`. To say no or maybe, use `calendar decline` /
`calendar tentative`.
"""

CALENDAR_DECLINE_EPILOG = """
Examples:

\b
  # Decline one invitation, with a reason
  blumkin calendar decline --event-id AAMk... --comment "clashes with the board call" --yes
\b
  # Tentatively accept everything pending for today
  blumkin calendar tentative --today-pending --yes
\b
  # Decline and propose another time (Microsoft only)
  blumkin calendar decline --event-id AAMk... \\
    --propose-time 2026-09-02T15:00 --propose-duration 45m --yes

Sends a response to each organizer, so `--yes` is required. `--comment` reaches
the organizer. `--propose-time` / `--propose-duration` work on Microsoft only
(Google Calendar has no propose-new-time - the command fails closed there);
they need a single `--event-id`. `--today-pending` batches like
`calendar accept`, reporting any events it had to skip.
"""

CALENDAR_CANCEL_EPILOG = """
Example:

\b
  blumkin calendar cancel --event-id AAMk... --yes

Sends a cancellation to every attendee (requires `--yes`). Only the organizer can
cancel an event; an attendee who wants out declines it in their calendar client.
"""

CALENDAR_CREATE_EPILOG = """
Examples:

\b
  # 30-minute Teams 1:1 (Teams link added by default)
  blumkin calendar create --subject "1:1 sync" --with sam@example.com \\
    --start "2026-09-01T15:00" --duration 30m --yes
\b
  # Two attendees, one hour, explicit timezone
  blumkin calendar create --subject "Design review" \\
    --with sam@example.com --with dana@example.com \\
    --start "2026-09-02T09:00" --duration 1h --tz America/New_York --yes
\b
  # Offline hold on your own calendar, no Teams link
  blumkin calendar create --subject "Focus block" --with me@example.com \\
    --start "2026-09-02T13:00" --duration 2h --no-teams --yes
\b
  # Solo hold with a reminder a day ahead (email on Google, popup on Outlook)
  blumkin calendar create --subject "Review renewal" \\
    --start "2026-09-28T10:00" --remind-email 1d --no-teams --yes
\b
  # Weekly recurring 1:1, ending on a date
  blumkin calendar create --subject "Henrique/Sam 1:1" --with sam@example.com \\
    --start "2026-09-22T13:05" --duration 45m \\
    --repeat weekly --until "2026-12-31" --yes
\b
  # Every-weekday lunch hold for the next 20 working days, no Teams link
  blumkin calendar create --subject "Lunch" --start "2026-09-22T12:00" \\
    --duration 1h --repeat weekly --days mon,tue,wed,thu,fri --count 20 \\
    --no-teams --yes
\b
  # Design review with an agenda, a location, and an optional attendee
  blumkin calendar create --subject "Design review" --with sam@example.com \\
    --optional dana@example.com --start "2026-09-22T09:00" --duration 1h \\
    --location "Room 4" --body "Agenda: API shape, timeline" --yes
\b
  # All-day out-of-office hold, three days, no Teams link
  blumkin calendar create --subject "OOO" --start "2026-12-24" --all-day \\
    --duration 3d --no-teams --yes

Invites every `--with` address, so `--yes` is required (still required with no
attendees). `--optional` attendees are invited too but marked optional.
`--body` / `--body-file` set the agenda (`--body-type` html/text is Microsoft
only). `--location` is free text. `--all-day` makes `--start` a date and
`--duration` whole days, and never attaches a Teams link (an all-day online
meeting is rejected); a date-only `--start` without `--all-day` is an error.
`--remind-email` adds an email reminder on Google and
an Outlook popup reminder on Microsoft. For a cross-timezone or external
attendee, run `calendar freebusy` or `calendar suggest` first and pick a slot
inside their working hours. `--start` stays in the organizer timezone.

`--repeat {daily,weekly,monthly}` makes a recurring series (Graph
patternedRecurrence / Google RRULE). Bound it with `--until DATE` or `--count N`
(omit both for an open-ended series), widen the gap with `--interval N`, and for
weekly patterns restrict the weekdays with `--days mon,tue,...` (which must
include the `--start` weekday). Monthly repeats on the same day-of-month as
`--start`.
"""

CALENDAR_EPILOG = """
Common workflows:

\b
  # What's on today / this week
  blumkin calendar today --json
  blumkin calendar view --from 2026-09-01 --to 2026-09-08 --json
\b
  # Find and book a mutual slot
  blumkin calendar suggest --with sam@example.com --with dana@example.com \\
    --start "2026-09-01T09:00" --end "2026-09-03T18:00" --duration 45m --json
  blumkin calendar create --subject "Planning" --with sam@example.com \\
    --start "2026-09-01T14:00" --duration 45m --yes
\b
  # Clear today's pending invitations
  blumkin calendar accept --today-pending --yes

Times are local to the organizer (config `default_tz`, or `--tz AREA`). Ranges
are half-open: `view --from D1 --to D2` excludes D2.
"""

CALENDAR_GET_EPILOG = """
Example:

\b
  blumkin calendar get --event-id AAMk... --json
  blumkin calendar get --event-id AAMk... --body-type html

Returns the full event: body/agenda, every attendee with their response, the
recurrence (same shape `calendar create --json` emits), the online-meeting join
URL, and `series_master_id` when it is one instance of a recurring series.
Read-only. `--body-type` is Microsoft-only (Graph converts server-side); Google
returns its single stored description, which may contain HTML. `--calendar
NAME|ID` reads from a non-default calendar (see `calendar list`).
"""

CALENDAR_LIST_EPILOG = """
Example:

\b
  blumkin calendar list --json

Lists every calendar the account can see: `id`, `name`, `is_default`,
`can_edit`, `owner`, `color`. Pass a `name` or `id` from here to `--calendar` on
`calendar today` / `view` / `get` / `create` / `update` / `cancel` to target a
non-default calendar (an ambiguous name is a usage error - use the id).
Read-only, no extra scope.
"""

CALENDAR_FREEBUSY_EPILOG = """
Example:

\b
  blumkin calendar freebusy --with sam@example.com --with dana@example.com \\
    --start "2026-09-01T09:00" --end "2026-09-01T18:00" --json

Returns busy intervals (not free slots) for each person, plus their timezone and
working hours when Graph exposes them. To get ranked mutual-free start times,
use `calendar suggest` instead. Do not use `--with` to guess someone's address -
resolve it first with `blumkin people resolve`.
"""

CALENDAR_SUGGEST_EPILOG = """
Examples:

\b
  # Ranked 45-minute slots where everyone (incl. you) is free
  blumkin calendar suggest --with me@example.com --with sam@example.com \\
    --start "2026-09-01T09:00" --end "2026-09-03T18:00" --duration 45m --json
\b
  # Clip to a working-day window, count tentative blocks as free
  blumkin calendar suggest --with sam@example.com \\
    --start "2026-09-01T09:00" --end "2026-09-01T18:00" \\
    --duration 30m --window 09:00-17:00 --treat-tentative free --json

Only suggests starts; it never creates an event. Feed a chosen start straight
into `calendar create`.
"""

CALENDAR_TODAY_EPILOG = """
Examples:

\b
  blumkin calendar today --json
  blumkin calendar today --date 2026-09-01 --tz America/Los_Angeles

Lists events for the local day. Graph returns UTC; blumkin converts to `--tz`
(or the config default). Use `--json` to get event ids for accept/cancel/update.
"""

CALENDAR_UPDATE_EPILOG = """
Examples:

\b
  # Move a meeting and change its length
  blumkin calendar update --event-id AAMk... --start "2026-09-23T14:00" \\
    --duration 45m --yes
\b
  # Edit the agenda and location only
  blumkin calendar update --event-id AAMk... --location "Room 7" \\
    --body "Updated agenda: …" --yes
\b
  # Replace the attendee list and remove the online meeting
  blumkin calendar update --event-id AAMk... --with sam@example.com \\
    --with dana@example.com --no-teams --yes

Only the flags you pass are changed. `--with` **replaces** the whole attendee
list. `--teams` attaches an online meeting, `--no-teams` removes it, omit to
leave it. `--all-day` / `--no-all-day` convert the event type (`--start` becomes
a date, `--duration` whole days). `--start` alone keeps the current length;
`--duration` or `--end` (not both) sets a new one. Editing a recurring series
edits the whole series. Uses Calendars.ReadWrite; requires `--yes`.
"""

CALENDAR_VIEW_EPILOG = """
Examples:

\b
  # A Mon..Sun week (2026-08-31 is a Monday; the following Monday is excluded)
  blumkin calendar view --from 2026-08-31 --to 2026-09-07 --json
\b
  # A single day
  blumkin calendar view --from 2026-09-01 --to 2026-09-02

The range is half-open `[--from, --to)`: `--to` is the first day NOT shown.
"""

CHAT_ATTACHMENTS_DOWNLOAD_EPILOG = """
Examples:

\b
  # One file to an explicit path
  blumkin chat attachments download --with "Sam Rivera" --latest \\
    --attachment-id 01ABC... --out ./contract.docx
\b
  # Every file from the newest message with attachments, into a folder
  blumkin chat attachments download --with "Sam Rivera" --latest --all \\
    --out ./downloads/

Teams chat files live in SharePoint/OneDrive, so download needs the `files_scopes`
opt-in. Without it, listing still works and download exits 4 (missing_scope) with
a share URL to open in Teams.
"""

CHAT_ATTACHMENTS_EPILOG = """
Examples:

\b
  # List files on the newest message that carries any
  blumkin chat attachments --with "Sam Rivera" --latest --json
\b
  # List files on a specific message
  blumkin chat attachments --chat-id 19:abc... --message-id 17... --json

Pass exactly one of `--chat-id` / `--with`, and one of `--message-id` /
`--latest`. Use the `download` subcommand to fetch bytes.
"""

CHAT_DELETE_EPILOG = """
Example:

\b
  blumkin chat delete --chat-id 19:abc... --message-id 17... --yes

Soft-deletes one of your messages; every participant sees it vanish, so `--yes`
is required. Needs `wo1162425_scopes = true` (Chat.ReadWrite).
"""

CHAT_EDIT_EPILOG = """
Example:

\b
  blumkin chat edit --chat-id 19:abc... --message-id 17... \\
    --text "Updated: moving the sync to 3pm" --yes

Rewrites a message other people have already read, so `--yes` is required. Needs
`wo1162425_scopes = true` (Chat.ReadWrite).
"""

CHAT_EPILOG = """
Common workflows:

\b
  # Read the last few messages from a 1:1
  blumkin chat find --with "Sam Rivera" --json
  blumkin chat last --with "Sam Rivera" --n 5 --json
\b
  # Send a message (notifies the recipient)
  blumkin chat send --with "Sam Rivera" --text "On my way" --yes

Reads work with the base scope set. Writes (send/edit/delete) and needing a
specific chat id require `wo1162425_scopes = true`. When `--with` is ambiguous,
pass `--chat-id` from `chat find`.
"""

CHAT_FIND_EPILOG = """
Example:

\b
  blumkin chat find --with "Sam" --json

Lists chats whose members match the substring. Use it to get a `--chat-id` when a
display name matches more than one chat.
"""

CHAT_LAST_EPILOG = """
Examples:

\b
  blumkin chat last --with "Sam Rivera" --json
  blumkin chat last --with "Sam Rivera" --n 10 --json
  blumkin chat last --chat-id "19:...@unq.gbl.spaces" --n 10 --json
  blumkin chat last --with "Sam Rivera" --contains "admin access" --n 5 --json

Shows the last N messages (default 3) from one chat. Pass exactly one of
`--with` (display-name substring) or `--chat-id` (from `chat find`).

`--contains` filters message bodies case-insensitively over a newest-first
local scan (Graph has no $search on chat messages), the same shape as
`mail list --from` / `--subject`. The scan stops after 500 messages;
`filters.complete` is false when it did, so an empty result means "not in the
recent N", not "does not exist". Without `--contains`, `filters.scanned` and
`filters.complete` stay null.

Exit 5 (not_found) means no chat matched `--with`. Exit 2 (usage_error) means
`--with` matched several chats - the message lists their ids, so re-run with
`--chat-id <id>` rather than guessing which one you got.
"""

CHAT_SEND_EPILOG = """
Examples:

\b
  # By display name
  blumkin chat send --with "Sam Rivera" --text "Sending the deck now" --yes
\b
  # By explicit chat id when the name is ambiguous
  blumkin chat send --chat-id 19:abc... --text "Thanks!" --yes

Messages a real person, so `--yes` is required. Needs `wo1162425_scopes = true`.
Use ASCII hyphens in `--text`, not em dashes.
"""

COMPLETION_EPILOG = """
Easiest: let blumkin write the file to the per-user completion dir for your
shell (idempotent; --force overwrites a differing file; --json reports the path):

\b
  blumkin completion bash --install
  blumkin completion zsh --install   # then put its dir on $fpath before compinit
  blumkin completion fish --install

Or place it yourself. bash-completion v2 lazy-loads this path - no rc edit, no
re-source:

\b
  blumkin completion bash > ~/.local/share/bash-completion/completions/blumkin.bash
\b
  # fish
  blumkin completion fish > ~/.config/fish/completions/blumkin.fish
\b
  # source-from-rc way (bash)
  blumkin completion bash > ~/.blumkin-complete.bash
  echo 'source ~/.blumkin-complete.bash' >> ~/.bashrc
\b
  # source-from-rc way (zsh)
  blumkin completion zsh > ~/.blumkin-complete.zsh
  echo 'source ~/.blumkin-complete.zsh' >> ~/.zshrc

Open a new shell afterwards. The script calls back into `blumkin` at completion
time via the _BLUMKIN_COMPLETE env var, so keep `blumkin` on PATH.
"""

DOCS_CREATE_EPILOG = """
Examples:

\b
  # Short single-line body (a heading and a sentence)
  blumkin docs create --title "Kickoff note" \\
    --body "# Kickoff. First meeting is Monday at 10." --json
\b
  # Multi-line Markdown - use --body-file (a shell-quoted --body keeps \\n literal)
  blumkin docs create --title "Weekly status" --body-file ./status.md \\
    --folder "Language Classes/Portuguese Classes" --json

Creates a real Google Doc on `provider = "google"`; a `.docx` uploaded to your
OneDrive on `provider = "microsoft"`. No `--yes` - nobody is notified.

`--folder` is a path: an existing folder (including one you made by hand) or one
created on the spot, intermediate folders included. An ambiguous segment on
Google is exit 2 - pass the folder id via `blumkin drive move` instead.

The Markdown subset is headings, bold / italic / code / links, bullet and
numbered lists, fenced code, rules, and simple tables; anything else renders as
plain text. Use ASCII hyphens, not em dashes.
"""

DOCS_EPILOG = """
Author a document and store it in your drive:

\b
  blumkin docs create --title "..." --body-file ./brief.md --json
  blumkin docs update --id 1AbC... --body-file ./brief-v2.md --json

One authoring format (a Markdown subset) across both providers; the backend is a
native Google Doc, or a `.docx` uploaded to OneDrive. `update` re-renders a doc
this tool created in place - same id, URL, and sharing.
"""

DOCS_UPDATE_EPILOG = """
Examples:

\b
  # Rename only
  blumkin docs update --id 1AbC... --title "Kickoff note (final)" --json
\b
  # Replace the whole body from a file
  blumkin docs update --id 1AbC... --body-file ./status-v2.md --json
\b
  # Rename and re-render in one call
  blumkin docs update --id 1AbC... --title "Q3 status" --body-file ./q3.md --json

Updates a doc this tool created: a real Google Doc on `provider = "google"`, the
uploaded `.docx` on `provider = "microsoft"`. The id, URL, and sharing are
unchanged. No `--yes` - nobody is notified.

Pass at least one of `--title`, `--body`, or `--body-file`. `--body` replaces the
*entire* body (no partial or range edits) - any manual edits made in the document
since it was created are overwritten.

`--id` must be a document this blumkin install created: `docs create` records the
id locally, and `docs update` refuses anything else with exit 5 (not_found) - the
Graph / Google Docs write scopes cover more than blumkin's own files, so this
guard is what stops a stray `--id` from clobbering another document. A doc
created on a different machine is only updatable on Google (a `drive.file` lookup
still recognises it); on Microsoft, open it in the browser. Use ASCII hyphens,
not em dashes.
"""

DOCTOR_EPILOG = """
Examples:

\b
  blumkin doctor
  blumkin doctor --json

Checks that the client id is set, the token cache / auth record exist, and
reports which scope set is active. Exit 3 (auth_required) lists the problems to
fix (usually: run `blumkin auth login`).

`install:` reports the detected install method (pipx / uv-tool / an editable
`-e` checkout / unmanaged). A warning fires when the baked `.dist-info` version
is behind the checkout's `pyproject.toml` - a `git pull` without a reinstall;
`blumkin upgrade` prints the fix.
"""

DRIVE_EPILOG = """
Read and organize your drive:

\b
  blumkin drive list --folder "Language Classes/Portuguese Classes" --json
  blumkin drive get --id 1a2b3c... --json
  blumkin drive read --id 1a2b3c... --json
  blumkin drive export --id 1a2b3c... --to ./doc.pdf

A Google Doc reads back as Markdown; other files export to PDF (and more, on
Google).

Provider-neutral verbs; help text names OneDrive on the Microsoft side. These
need `docs_scopes = true` on `provider = "microsoft"` (Files.ReadWrite - the same
grant `docs create` uses; there is no separate drive toggle). On
`provider = "google"` re-run `blumkin auth login` once to consent to `drive`.
"""

DRIVE_LIST_EPILOG = """
Examples:

\b
  # A folder by native path (Microsoft) or best-effort name walk (Google)
  blumkin drive list --folder "Reports/2026" --json
\b
  # A folder by id (unambiguous on both providers)
  blumkin drive list --folder-id 1a2b3c... --order name --json
\b
  # Search a folder (or the whole drive with no --folder*)
  blumkin drive list --query "portuguese vocab" --top 20 --json

`--json` items have a stable cross-provider shape: `{ id, name, mime_type, kind,
size, modified, web_url, parent_id, provider }` where `kind` is one of
file / folder / doc / sheet / slides. `--top 0` follows every page.
"""

DRIVE_GET_EPILOG = """
Examples:

\b
  blumkin drive get --id 1a2b3c... --json

Full metadata for one item: owners, parents, timestamps, `web_url`, and the
export formats available (`drive export`). Ids come from `blumkin drive list`.
"""

DRIVE_DOWNLOAD_EPILOG = """
Examples:

\b
  blumkin drive download --id 1a2b3c... --out ./report.xlsx
  blumkin drive download --id 1a2b3c... --out ./drive-files/

`--out` is a file (refuses to overwrite) or a directory (keeps the drive name).
Google-native docs (Docs / Sheets / Slides) have no raw bytes - exit 2 with a
pointer to `drive export`. `--out` is a path on the host running the skill.
"""

DRIVE_EXPORT_EPILOG = """
Examples:

\b
  blumkin drive export --id 1a2b3c... --to ./vocab.pdf
  blumkin drive export --id 1a2b3c... --to ./vocab.txt --json

The `--to` extension selects the format. Google exports Docs / Sheets / Slides to
pdf / txt / html / csv / docx / xlsx / pptx; Microsoft (OneDrive) honours `pdf`
only - any other extension is exit 2. `--to` is a path on the host running the
skill and must not already exist.
"""

DRIVE_READ_EPILOG = """
Examples:

\b
  blumkin drive read --id 1a2b3c... --json

Flattens a Google Doc to the `docs create` Markdown subset (headings, bold /
italic / code / links, bullet and numbered lists). `provider = "google"` only -
on Microsoft this is exit 2 (`usage_error`); use `drive export --to out.pdf`.
"""

DRIVE_MKDIR_EPILOG = """
Examples:

\b
  blumkin drive mkdir --path "Reports/2026/Q3" --yes --json

`mkdir -p` semantics: missing intermediate folders are created. If the folder
already exists it is a no-op (`created: false`). Requires `--yes`.
"""

DRIVE_MOVE_EPILOG = """
Examples:

\b
  # Into an existing folder, by path
  blumkin drive move --id 1a2b3c... --to "Language Classes/Portuguese Classes" --yes
\b
  # Into a folder by path, creating missing parents
  blumkin drive move --id 1a2b3c... --to "Archive/2026" --make-parents --yes --json

Reparents a file or folder; its id, URL, and sharing do not change. Pass exactly
one of `--to` (a path) or `--to-id` (a folder id). A `--to` path that does not
exist is exit 2 unless `--make-parents` is given. Requires `--yes`.
"""

DRIVE_RENAME_EPILOG = """
Examples:

\b
  blumkin drive rename --id 1a2b3c... --name "Portuguese vocab - week 4" --yes

Renames in place - id, URL, and sharing are unchanged. Requires `--yes`.
"""

MAIL_ATTACHMENTS_DOWNLOAD_EPILOG = """
Examples:

\b
  # One attachment by id
  blumkin mail attachments download --message-id AAMk... \\
    --attachment-id AAMk...= --out ./invoice.pdf
\b
  # Every file attachment into a directory
  blumkin mail attachments download --message-id AAMk... --all --out ./mail-files/

Get attachment ids from `blumkin mail attachments --id AAMk... --json`.
"""

MAIL_ATTACHMENTS_EPILOG = """
Example:

\b
  blumkin mail attachments --id AAMk... --json

Lists attachments (name, size, id) on one message. Use the `download` subcommand
to save them.
"""

MAIL_DELETE_DRAFT_EPILOG = """
Example:

\b
  blumkin mail delete-draft --id AAMk...

Permanently removes a draft. No `--yes` needed - nobody is notified. Safe way to
clean up after inspecting a draft you created for testing.
"""

MAIL_DRAFT_EPILOG = """
Examples:

\b
  # Markdown draft to two people (the default: bold, lists, links all render)
  blumkin mail draft --to sam@example.com --to dana@example.com \\
    --subject "Notes from today" --body-file ./recap.md
\b
  # Raw HTML body from a file, with an attachment, skipping the config signature
  blumkin mail draft --to sam@example.com --subject "Q3 deck" \\
    --body-file ./note.html --body-type html --attach ./q3.pdf --no-signature

Creates the draft only; send it with `mail send-draft --id ... --yes`. `--body`
is authored as Markdown by default and rendered to HTML on the wire, so the
message keeps its structure in Gmail and Outlook; pass `--body-type text` for a
literal plain-text body. `--to` / `--cc` / `--bcc` repeat or take comma-separated
lists. Keep attachments under 2 MB each. Use ASCII hyphens in the body, not em
dashes.
"""

MAIL_EPILOG = """
Common workflows:

\b
  # Triage the inbox
  blumkin mail inbox --unread --top 20 --json
  blumkin mail inbox --from sam --since 2026-08-01 --json
\b
  # Read one message in full (participants, body, attachments)
  blumkin mail get --id AAMk... --json
\b
  # Draft a reply that threads correctly, then send it
  blumkin mail reply --id AAMk... --body "Works for me - see you then."
  blumkin mail send-draft --id AAMk... --yes

All drafting verbs stay in your mailbox until `mail send-draft --yes`. `--from` /
`--subject` filter locally over a newest-first scan (max 500); `--search` is
Graph server-side and cannot combine with those filters.
"""

MAIL_FOLDERS_EPILOG = """
Example:

\b
  blumkin mail folders --json

Lists folder ids and message counts, including custom folders. Graph's totals can
lag - do not treat `total: 0` as proof a folder is empty; confirm with
`mail list --folder NAME`.
"""

MAIL_FORWARD_EPILOG = """
Example:

\b
  blumkin mail forward --id AAMk... --to dana@example.com \\
    --body "Forwarding for your records - see the thread below."

Creates a forward draft (does not send). Pass `--body` on create; filling it in
later with `mail update-draft --body` replaces the quoted original. `--body` is
Markdown by default, rendered to HTML (use `--body-type text` for plain). `--cc`
/ `--bcc` on create merge with inherited recipients.
"""

MAIL_GET_EPILOG = """
Examples:

\b
  blumkin mail get --id AAMk... --json
  blumkin mail get --id AAMk... --body-type html

Fetches one message in full. Prefer this over listing and filtering client-side
when you already have the id. Default body type is text.
"""

MAIL_TRIAGE_EPILOG = """
Examples:

\b
  blumkin mail move --id AAMk... --to archive --yes
  blumkin mail move --id AAMk... --id AAMk2... --to "Receipts" --yes
  blumkin mail mark --id AAMk... --read --flag --yes
  blumkin mail mark --id AAMk... --importance high --yes
  blumkin mail delete --id AAMk... --yes

`move` / `mark` / `delete` change mailbox state but notify no one, so `--yes` is
a safety confirm, not a notify gate. `--id` is repeatable; a batch reports each
message it skipped rather than aborting. `delete` goes to Deleted Items / Gmail
Trash (recoverable). `move --to archive` just removes the Inbox label. Other
targets: a well-known name (`deleteditems`, `sentitems`, ...), a folder id, or a
folder display name. Microsoft matches the name against your folder tree and
falls back to treating an unmatched token as a folder id; Google resolves a
Gmail label name to its id (an unmatched label name is `not_found`, exit 5) or
takes a `Label_` id from `mail folders` directly.

Microsoft: covered by `Mail.ReadWrite` (already granted). Google: needs
`gmail.modify` - a NEW scope. Until you re-run `blumkin auth login` and grant it,
these exit 4 (`missing_scope`). `gmail`'s `flag` maps to the STARRED label and
`importance` to the IMPORTANT label.
"""

MAIL_AUTO_REPLY_EPILOG = """
Examples:

\b
  blumkin mail auto-reply --json
  blumkin mail auto-reply --on --message "Out until Sept 15, back then." --yes
  blumkin mail auto-reply --on --message-file oof.txt --start 2026-09-10 \\
    --until 2026-09-15 --external contacts --yes
  blumkin mail auto-reply --off --yes

With no flags this reads the current setting; passing a change flag
(`--message`, `--start`, ...) without `--on` / `--off` is a usage error, not a
silent read. `--on` needs `--message` or `--message-file`; `--off` clears it.
Both need `--yes`. `--start` / `--until` schedule a window (otherwise it stays
on until you turn it off); the dates are read in the profile timezone
(`default_tz`) and `--until` is inclusive - the whole end day is covered.
`--external` picks who outside your org gets a reply: `all` (default),
`contacts`, or `none`.

The message is authored as plain text; on Microsoft its newlines are converted
to `<br>` (Exchange stores the OOF body as HTML), so a multi-line
`--message-file` keeps its line breaks in Outlook.

Microsoft splits internal and external bodies - `--external-message` sets a
separate one; it needs `wo1162425_scopes` (MailboxSettings.ReadWrite). Google
has a single body (so `--external-message` is rejected); `--external contacts`
maps to Gmail's `restrictToContacts` and `--external none` to `restrictToDomain`
(org only). Google needs the NEW `gmail.settings.basic` scope, so until you
re-run `blumkin auth login` it exits 4 (`missing_scope`).
"""

MAIL_SEARCH_EPILOG = """
Examples:

\b
  blumkin mail search --query "renewal from:dana" --json
  blumkin mail search --query invoice --since 2026-08-01 --until 2026-09-01 --json

Searches the WHOLE mailbox (every folder), relevance-ranked, and tags each hit
with its `folder`. `mail list --search` only covers one folder. Microsoft uses
Graph `$search`, Google uses Gmail `q=` (so Gmail operators like `from:` /
`subject:` / `has:attachment` work there). Graph `$search` cannot combine with a
server-side date filter, so with `--since` / `--until` it over-fetches a
relevance window and filters locally - `complete` is then `null` (a match
outside the window cannot be ruled out), and a hit with no timestamp (a draft)
is dropped rather than passed. Graph `$search` has no escape for a `"` inside
the query, so a quoted phrase is rejected on Microsoft (it works on Gmail
`q=`). No extra scope.
"""

MAIL_THREAD_EPILOG = """
Examples:

\b
  blumkin mail thread --id AAMk... --json
  blumkin mail thread --id AAMk... --full --body-type text

Lists every message in the conversation the given message belongs to, oldest
first, each with the `mail list` summary shape. `--full` adds each body
(`--body-type` html/text; Microsoft converts server-side, Google returns its
stored representation). Microsoft resolves the `conversationId` and filters
`/me/messages`; Google reads the Gmail thread. No extra scope.
"""

MAIL_INBOX_EPILOG = """
Examples:

\b
  # Recent unread
  blumkin mail inbox --unread --top 20 --json
\b
  # From a sender, since a date (half-open [since, until))
  blumkin mail inbox --from "sam@example.com" --since 2026-08-01 --json
\b
  # High-importance mail with an attachment (both server-side)
  blumkin mail inbox --importance high --has-attachments --json
\b
  # Full-text search across the whole mailbox (server-side)
  blumkin mail inbox --search "quarterly report" --json

`--from` / `--subject` match locally over a newest-first scan capped at 500
messages (payload then says `complete: false`). `--importance` /
`--has-attachments` filter server-side and keep the sort. `--search` runs on
Graph and cannot be combined with `--from` / `--subject` / date / importance /
attachment filters.
"""

MAIL_LIST_EPILOG = """
Examples:

\b
  # Sent items, newest first
  blumkin mail list --folder sentitems --top 20 --json
\b
  # A custom folder by display name, only unread
  blumkin mail list --folder "Receipts" --unread --json
\b
  # Archive, high-importance only
  blumkin mail list --folder archive --importance high --json

`--folder` takes a well-known name (inbox, sentitems, drafts, archive,
deleteditems, junkemail, outbox), a folder id, or a custom folder's display
name. Sort defaults by folder (sent for Sent Items, created for Drafts/Outbox,
received otherwise); override with `--orderby`. `--importance` /
`--has-attachments` filter server-side; same `--search` exclusivity as
`mail inbox`.
"""

MAIL_REPLY_EPILOG = """
Examples:

\b
  # Reply to the sender, threaded, with body text
  blumkin mail reply --id AAMk... --body "Confirmed for Tuesday at 10."
\b
  # Reply-all, adding a CC
  blumkin mail reply --id AAMk... --all --cc lead@example.com \\
    --body "Looping in the lead."

Prefer this over a fresh draft with "RE:" - Graph keeps it in the original
conversation and inherits recipients. Draft only; send with `mail send-draft
--yes`. Pass `--body` on create (Markdown by default, rendered to HTML above the
quoted thread; `--body-type text` for plain); a later `mail update-draft --body`
drops the quoted original.
"""

MAIL_SEND_DRAFT_EPILOG = """
Example:

\b
  blumkin mail send-draft --id AAMk... --yes

Sends an existing draft (from `mail draft` / `mail reply` / `mail forward`).
Requires `--yes` - this is the step that actually delivers mail.
"""

MAIL_SIGNATURE_EPILOG = """
Examples:

\b
  blumkin mail signature --json
  blumkin mail signature --body-type text

Prints the rendered [mail.signature] for the active profile - the same markup
the drafting verbs append - so you can add it to a body you are composing
yourself without hand-rebuilding the styling from config. Read-only; empty when
no signature is configured or it is disabled.

Double-signature guard: Outlook (desktop / web) can be set to auto-insert its
own signature on new mail and replies, and Graph exposes no API for that
setting. `blumkin auth login` and `blumkin doctor` probe for it (create a
throwaway draft, read it back, delete it). When it is on, blumkin stops
appending [mail.signature] to drafts and this command reports `suppressed: true`
- otherwise a draft blumkin leaves in the mailbox is signed twice once Outlook
touches it. Re-run `blumkin doctor` after changing the Outlook setting.
"""

MAIL_UPDATE_DRAFT_EPILOG = """
Examples:

\b
  # Add an attachment to an existing draft (adds, never replaces)
  blumkin mail update-draft --id AAMk... --attach ./addendum.pdf
\b
  # Replace the whole recipient list and subject
  blumkin mail update-draft --id AAMk... --to sam@example.com \\
    --to dana@example.com --subject "Revised: Q3 deck"
\b
  # Rewrite your half of a reply, keeping the quoted thread below it
  blumkin mail update-draft --id AAMk... --body-file ./reply.html \\
    --body-type html --keep-quoted

No `--yes` (stays in your mailbox). `--to` / `--cc` / `--bcc` and `--body` each
replace that field wholesale when given - include every value that should remain.
`--attach` is additive.

Replacing the body reapplies `[mail.signature]` (pass `--no-signature` to skip),
so an edited reply keeps the same sign-off the drafting verbs add. `--keep-quoted`
re-appends the quoted original from the existing draft after your new text, so
editing a reply does not drop the thread; the result is sent as HTML, since the
quoted block is markup. See also `blumkin mail signature`.
"""

MAIN_EPILOG = """
Getting started:

\b
  blumkin auth login                       # once per machine
  blumkin profiles list --json             # which accounts are configured
  blumkin skills list --json               # what blumkin can do
  blumkin doctor                           # check config + token cache

Everyday reads:

\b
  blumkin calendar today --json
  blumkin mail inbox --unread --top 20 --json
  blumkin chat last --with "Sam Rivera" --n 5 --json

Notes:

\b
  - Add --json to any command for machine-readable output (best for agents).
  - Writes that notify someone (invites, sends, chats) require --yes.
  - Times use the profile default_tz unless you pass --tz AREA (IANA name).
  - Multiple profiles: pass --profile NAME (a profile name or tag) or set BLUMKIN_PROFILE.
  - Config + token cache: $BLUMKIN_CONFIG_DIR, else ~/.config/blumkin/.
  - Exit codes: 0 ok, 2 usage, 3 auth_required, 4 missing_scope, 5 not_found.

Per-command help: blumkin COMMAND --help (e.g. blumkin calendar create --help).
"""

MCP_EPILOG = """
Run blumkin as a Model Context Protocol stdio server, so MCP-aware agents call
each skill as a typed tool instead of shelling out and parsing --help.

Example:

\b
  claude mcp add --transport stdio blumkin -- blumkin mcp serve
  # or in .cursor/mcp.json / ~/.copilot/mcp-config.json:
  #   { "command": "blumkin", "args": ["mcp", "serve"] }

Needs the optional `mcp` extra: `pipx install 'blumkin[mcp]'` (or
`uv tool install 'blumkin[mcp]'`). Auth stays a CLI step - run `blumkin auth
login` on a TTY once; the server shares the same token cache.
"""

MCP_SERVE_EPILOG = """
Example:

\b
  blumkin mcp serve
  blumkin mcp serve --profile work --read-only
  blumkin mcp serve --only calendar --only mail

Blocks, speaking JSON-RPC on stdin/stdout, until the client disconnects (the
host spawns and reaps it per session - there is no daemon). Every skill with a
worker method is exposed as a tool named by its id; the CLI-only verbs (`auth
*`, `doctor`, `skills *`, `mail signature`, `mcp serve`) are not. Tools whose
CLI form needs `--yes` require a `confirm: true` argument the server enforces.
`--read-only` drops every mutating tool; `--only PREFIX` (repeatable) keeps only
tools under that id prefix.

Accounts: without `--profile`, a server with two or more configured profiles
requires a `profile` argument on every tool call (and exposes a read-only
`profiles.list` tool plus guidance to ask the user when the account is
ambiguous); with one profile it is optional. `--profile NAME` pins the server to
one account and drops the `profile` argument. `--read-only` / `--only` stay
server-scoped - for "personal read-only, work read-write" run a second pinned
`mcp serve --profile personal --read-only` alongside.
"""

MCP_INSTALL_EPILOG = """
Example:

\b
  blumkin mcp install                       # detect clients, ask, confirm each
  blumkin mcp install --scope user          # every repo (default when asked)
  blumkin mcp install --client cursor --read-only --scope project
  blumkin mcp install --yes --scope user    # non-interactive; needs --scope

Detects Claude Code, Cursor, and GitHub Copilot CLI. For Claude and Copilot
(user scope) it calls the client's own `mcp add`; for Cursor and for project
scope it merges the entry into the JSON config, leaving any other servers alone.
Re-running is safe: an entry that already matches is reported "already current",
a stale one is updated. `--force` rewrites even a matching entry.

Auth is unchanged - run `blumkin auth login` on a TTY once; every registered
server shares that token cache. The `mcp` extra (`pipx install 'blumkin[mcp]'`)
is needed to *run* the server, not to install it here.
"""

MCP_STATUS_EPILOG = """
Example:

\b
  blumkin mcp status
  blumkin mcp status --json

Lists which agent CLIs are detected and, for each place `blumkin` is registered
(user or project scope), the exact command and args recorded there. A note flags
a registration whose command does not resolve to this blumkin (a stale path
after a reinstall - re-run `blumkin mcp install`).
"""

MEETING_EPILOG = """
Examples:

\b
  # Show online-meeting details for an event you organize
  blumkin meeting get --event-id AAMk...
\b
  # Show transcription flags, then enable them
  blumkin meeting transcription --event-id AAMk...
  blumkin meeting transcription --event-id AAMk... --enable --yes

Organizer-only. Needs `wo1162425_scopes = true` (OnlineMeetings.ReadWrite). Get
event ids from `blumkin calendar today --json`.
"""

MEETING_GET_EPILOG = """
Example:

\b
  blumkin meeting get --event-id AAMk... --json

Resolves the event's online meeting (join URL, id, settings). Exit 5 (not_found)
means the event has no online meeting or you are not the organizer.
"""

MEETING_TRANSCRIPTION_EPILOG = """
Examples:

\b
  blumkin meeting transcription --event-id AAMk...            # show flags
  blumkin meeting transcription --event-id AAMk... --enable --yes

`--enable` sets allowTranscription=true and needs `--yes`. Without `--enable` it
is a read.
"""

PEOPLE_CONTEXT_EPILOG = """
Examples:

\b
  blumkin people context --json
  blumkin people context --name Sam --json

Lists `~/.config/blumkin/email-context.md` (or
`$BLUMKIN_CONFIG_DIR/email-context.md`); the active profile's
`profiles/<name>/email-context.md` is read too and their entries combined. Both
a markdown table (`| Name | Aliases | Email | Notes |`) and bullet rows
(`- Sam (sammy) <sam@example.com> - note`) are read. Blumkin never writes the
file and never substitutes a name for an address on its own - look one up here,
then pass the real email. See docs/operator-config.md.
"""

PEOPLE_EPILOG = """
Examples:

\b
  # Name -> SMTP address
  blumkin people resolve --name "Sam Rivera" --json
\b
  # Reverse / exact-match check
  blumkin people resolve --email sam.rivera@example.com --json

Fail-closed: zero matches exits 5 (not_found); more than one exits 2 with
`ambiguous: true` and the candidates - ask which person, never guess. Needs
`wo1162425_scopes = true` (People.Read).
"""

PEOPLE_RESOLVE_EPILOG = PEOPLE_EPILOG

PROFILES_EPILOG = """
Examples:

\b
  blumkin profiles list --json

Shows each configured account (name, provider, timezone, tags) and which is the
default. Use a name or a unique tag with `--profile` on any command, e.g.
`blumkin --profile @personal calendar today`.
"""

PROFILES_LIST_EPILOG = PROFILES_EPILOG

PROFILES_SET_EMAIL_EPILOG = """
Examples:

\b
  # Backfill a profile that was signed in before blumkin tracked the address
  blumkin --profile work profiles set-email
\b
  # Set it explicitly (no API call)
  blumkin --profile personal profiles set-email --email me@example.com

`auth login` / `auth refresh` fill this in only when it is missing, so they never
relabel a profile on their own. This command overwrites: use it to backfill an
existing profile, or to resolve the drift `blumkin doctor` reports after a
profile is re-authenticated as somebody else.
"""

SKILLS_DESCRIBE_EPILOG = """
Example:

\b
  blumkin skills describe calendar.create --json

Shows one skill's CLI form, arguments, required scopes, and whether it mutates or
notifies others. Skill ids come from `blumkin skills list --json`.
"""

SKILLS_EPILOG = """
Examples:

\b
  blumkin skills list --json
  blumkin skills describe mail.draft --json

`skills list` is the machine-readable catalog of everything blumkin can do, with
a `notifies_others` flag per skill. Prefer `--json` in agent sessions.
"""

UPGRADE_EPILOG = """
Examples:

\b
  blumkin upgrade
  blumkin upgrade --yes
  blumkin upgrade --json

Detects how blumkin is installed and acts to match:

\b
  pipx (from PyPI)       -> pipx upgrade blumkin
  uv tool (from PyPI)    -> uv tool upgrade blumkin
  editable -e <path>     -> git pull --ff-only + a --force tool reinstall
  source checkout (uv)   -> git pull --ff-only + uv sync
  unmanaged venv/system  -> reported, not touched

An editable / source install prints those commands; `--yes` runs them. `from:` /
`to:` are the on-disk `blumkin --version` before and after. `--json` adds
`install_method`, `managed_path`, `checkout`, `metadata_stale`, `action_taken`,
and `suggested_commands`. Exit 1 (`upgrade_failed`) means a step exited non-zero
or a needed tool (pipx / uv / git) is missing.
"""
