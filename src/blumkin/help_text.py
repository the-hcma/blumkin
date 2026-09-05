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

Sends a response to each organizer, so `--yes` is required. Get event ids from
`blumkin calendar today --json`.
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

Invites every `--with` address, so `--yes` is required (still required with no
attendees). `--remind-email` adds an email reminder on Google and an Outlook
popup reminder on Microsoft. For a cross-timezone or external attendee, run
`calendar freebusy` or `calendar suggest` first and pick a slot inside their
working hours. `--start` stays in the organizer timezone.

`--repeat {daily,weekly,monthly}` makes a recurring series (Graph
patternedRecurrence / Google RRULE). Bound it with `--until DATE` or `--count N`
(omit both for an open-ended series), widen the gap with `--interval N`, and for
weekly patterns restrict the weekdays with `--days mon,tue,...`. Monthly repeats
on the same day-of-month as `--start`.
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
Example:

\b
  blumkin calendar update --event-id AAMk... --yes

Attaches a Teams online meeting to an event that does not have one (v1 only adds,
it cannot remove). Uses Calendars.ReadWrite. Requires `--yes` because attendees
are notified.
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
Enable completion (writes a file and sources it from your shell rc):

\b
  # bash
  blumkin completion bash > ~/.blumkin-complete.bash
  echo 'source ~/.blumkin-complete.bash' >> ~/.bashrc
\b
  # zsh
  blumkin completion zsh > ~/.blumkin-complete.zsh
  echo 'source ~/.blumkin-complete.zsh' >> ~/.zshrc
\b
  # fish
  blumkin completion fish > ~/.config/fish/completions/blumkin.fish

Open a new shell afterwards. The script calls back into `blumkin` at completion
time via the _BLUMKIN_COMPLETE env var, so keep `blumkin` on PATH.
"""

DOCTOR_EPILOG = """
Examples:

\b
  blumkin doctor
  blumkin doctor --json

Checks that the client id is set, the token cache / auth record exist, and
reports which scope set is active. Exit 3 (auth_required) lists the problems to
fix (usually: run `blumkin auth login`).
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
  # Plain-text draft to two people
  blumkin mail draft --to sam@example.com --to dana@example.com \\
    --subject "Notes from today" --body "Recap attached. Let me know if I missed anything."
\b
  # HTML body from a file, with an attachment, skipping the config signature
  blumkin mail draft --to sam@example.com --subject "Q3 deck" \\
    --body-file ./note.html --body-type html --attach ./q3.pdf --no-signature

Creates the draft only; send it with `mail send-draft --id ... --yes`. `--to` /
`--cc` / `--bcc` repeat or take comma-separated lists. Keep attachments under
2 MB each. Use ASCII hyphens in the body, not em dashes.
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
later with `mail update-draft --body` replaces the quoted original. `--cc` /
`--bcc` on create merge with inherited recipients.
"""

MAIL_GET_EPILOG = """
Examples:

\b
  blumkin mail get --id AAMk... --json
  blumkin mail get --id AAMk... --body-type html

Fetches one message in full. Prefer this over listing and filtering client-side
when you already have the id. Default body type is text.
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
--yes`. Pass `--body` on create; a later `mail update-draft --body` drops the
quoted original.
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
  blumkin upgrade --json

Wraps `pipx upgrade blumkin`. `from:` / `to:` are the pipx app's version and
commit before and after. Run from a source checkout it upgrades the pipx app and
reports the checkout separately, leaving the tree alone. Exit 1
(`upgrade_failed`) means pipx is missing or `pipx upgrade` exited non-zero.
"""
