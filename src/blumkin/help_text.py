"""Help and usage-example text for the blumkin CLI.

Kept out of ``cli.py`` so the command wiring stays skimmable. Every constant is
an ``epilog`` string passed to a Click group or command: it renders after the
options block. Paragraphs are rewrapped by Click unless they start with a ``\\b``
marker, so each preformatted example block is prefixed with ``\\b``.

Authoring style: ASCII hyphens only (no em/en dashes), matching
``.agents/skills/blumkin/SKILL.md``.
"""

from __future__ import annotations

AGENT_EPILOG = """
Examples:

\b
  blumkin agent status
  blumkin agent lock
  blumkin agent stop

The agent (issue #328/#339) is a background process that holds time-boxed,
decrypted credentials so blumkin only needs local presence re-verification
(Touch ID / device password) roughly once per TTL, instead of on every
command. `status` reports which profiles are currently cached (and flags
version skew after a `blumkin upgrade` - see issue #401); `lock` wipes
cached secrets (one profile, or all of them) without shutting the agent
down; `stop` shuts the agent process down entirely - it respawns on the
next command that needs it.
"""

AGENT_LOCK_EPILOG = """
Example:

\b
  blumkin agent lock

Drops the agent's cached secrets immediately instead of waiting for their
TTL to expire. The agent process itself keeps running - only its cached
state is wiped. A no-op if no agent is running - nothing is cached either
way.
"""

AGENT_STATUS_EPILOG = """
Examples:

\b
  blumkin agent status
  blumkin agent status --json

Shows whether the agent is currently running, and if so its pid, version,
and which profiles have cached state - without starting one just to check.
Warns when `agent_version` is older than this CLI's own build (issue #401)
- a `blumkin upgrade` swaps the package on disk but does not touch an
already-running agent, so run `blumkin agent stop` to retire it.

`orphaned_agents` (JSON) / `warning:` (text) additionally lists any other
reachable agent on a legacy socket path this build does not resolve to on
its own - e.g. one left running from before issue #402/#403's socket-path
fix (issue #408). Retire them with `blumkin agent stop --all`.
"""

AGENT_STOP_EPILOG = """
Examples:

\b
  blumkin agent stop
  blumkin agent stop --all
  blumkin agent stop --json

Shuts the agent process down entirely (unlike `lock`, which only wipes its
cached secrets and leaves it running). Useful after `blumkin upgrade` to
retire an agent still running the previous build's code (issue #401) - the
next command needing the agent spawns a fresh one from the just-upgraded
package. A no-op if no agent is running.

`--all` also stops every orphaned agent on a legacy socket path this build
does not resolve to on its own (issue #408) - the same ones `agent status`
surfaces under `orphaned_agents`.
"""

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

AUTH_UNLOCK_EPILOG = """
Example:

\b
  blumkin auth unlock

Unlocks the macOS login keychain by running `security unlock-keychain`, which
prompts for the password itself (blumkin never sees or stores it). Needs a TTY;
from an agent session run it with `! blumkin auth unlock`. Pair it with
`keychain_unlock = "cli"` in config.toml to fail fast, instead of hanging on the
GUI prompt, when the keychain is locked and there is no terminal (the default
"gui" does the same automatically over SSH / outside a GUI login session). An "Always
Allow" dialog for a new binary is a separate access-list prompt that unlocking
does not clear.
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

`account` is the signed-in account email, read from the cached `config.toml`
label written once at onboarding (`null` on a profile that has never
completed a login). `capabilities` (JSON) / `available:` (text) is the same
per-family usability summary as `blumkin doctor` and `blumkin capabilities`.

`next_steps` (JSON) / `next_step:` lines (text) name the exact command to run
for anything unhealthy - a missing client id, an incomplete token cache, a
scope gap, or an expired access token with no refresh token cached. Empty
when everything checked here is fine (still worth a `blumkin doctor` for the
checks this cache-only command intentionally skips, like a live email-drift
check or the OS keychain).
"""

AUTH_SETUP_EPILOG = """
Examples:

\b
  blumkin --profile personal auth setup
\b
  # First run in an empty config directory (creates config.toml, then sets up)
  blumkin --profile work auth setup --yes --provider microsoft \\
    --client-id 11111111-2222-3333-4444-555555555555 \\
    --tenant-id contoso.onmicrosoft.com --account-type organizational
\b
  # Non-interactive (agent-safe), ingesting a downloaded Desktop client JSON
  blumkin --profile personal auth setup --yes \\
    --from-file ~/path/to/google-oauth-desktop-client.json
\b
  # Non-interactive Microsoft setup
  blumkin --profile work auth setup --yes \\
    --client-id 11111111-2222-3333-4444-555555555555 \\
    --tenant-id contoso.onmicrosoft.com --account-type organizational

Walks through registering blumkin's OAuth app in the Google Cloud Console or
Microsoft Entra (whichever `provider` the active profile uses), then
validates and splits the values you provide: secrets go to the OS keychain
(same as `auth set-app-secret`), non-secret fields (`client_id`, endpoint
overrides, `tenant_id`, `account_type`) go to config.toml. The profile itself
(`[profiles.<name>]` with at least `provider` set) must already exist in
config.toml - this only fills in its OAuth client, it does not add a profile
to an existing config. The one exception is a first run: when the resolved
config directory has no config.toml at all, setup first creates the directory
(mode 0700) and a minimal config.toml (mode 0600) with the active profile and
its provider (asked for on a terminal, or `--provider google|microsoft` with
`--yes`). It never overwrites an existing config.toml.

Requires a TTY for the interactive walkthrough; pass `--yes` plus every
value's flag for a non-interactive/agent-driven run. Never accepts a secret
value as a command-line argument for the same reason as `auth set-app-secret`.
"""

AUTH_SET_APP_SECRET_EPILOG = """
Examples:

\b
  # Vault Google's client_secret straight from the Desktop client JSON
  blumkin auth set-app-secret --kind google_client_secret \\
    --from-file ~/path/to/google-oauth-desktop-client.json
\b
  # Vault Microsoft's client_id via stdin (scripting)
  echo -n "$MS_CLIENT_ID" | blumkin auth set-app-secret --kind ms_client_id --stdin
\b
  # Remove a vaulted value
  blumkin auth set-app-secret --kind google_client_secret --delete

Vaults an app secret in the OS keychain (issue #368) so it never has to live
in config.toml or a plaintext file. A vaulted value takes precedence over
the equivalent file/toml value (Google's Desktop client JSON client_secret;
Microsoft's toml client_id) - it does not replace or delete that source, so
leave-in-place is the default; delete it yourself once you have confirmed the
vaulted value works. Requires a usable OS keychain backend and
token_storage != "file" for the active profile.

Never pass the secret as a command-line argument - it would leak into shell
history and `ps`. Use `--from-file`, `--stdin`, or the interactive hidden
prompt (no flag, on a TTY).
"""

BROWSER_ADDRESS_EPILOG = """
Examples:

\b
  blumkin --profile work browser address expense-report
  blumkin --profile work browser address expense-report --json

Prints the physical address stored with `blumkin profiles set-address`, and only for a task
that declares `use_profile_address = true` under `[browser.tasks.TASK]` in config.toml; any
other task, or an unknown one, is refused before the address is looked at. The command only
returns the address: filling it into a page is a separate `browser fill`, so it reaches a
site only when you or the agent do that on a host the task allows. Treat the output as
sensitive.
"""

BROWSER_CLICK_EPILOG = """
Examples:

\b
  blumkin browser click expense-report "Add line"
  blumkin browser click expense-report "Add line" --role button --json
  blumkin browser click meeting-notes Transcript --wait 15
  blumkin browser click meeting-notes "More actions" --role button --nth 3

Clicks one control the task declared in `[browser.tasks.TASK] allowed_controls` in
config.toml (exact accessible names). Without `--role` the control is found by name
across the clickable roles and the role it was found in is reported; a name that exists
in more than one role is refused until you pass `--role`. The control is waited for up to
`--wait` seconds (default 5 by name, 30 with `--role`, at most 300), so a missing control by name
fails in seconds.
When the page legitimately repeats a name (a "More actions" button on every row), more than one
match is refused until `--nth N` (1-based, in DOM order, the order `browser read --aria` lists
them) says which one is meant; it narrows the lookup only, and the policy and the vetting of
that element are unchanged. `--nth` is for `click` only.
A declared name may contain `*` for any text (`"Actions for * Report"` covers a saved report's
actions link whatever the trip is called); a pattern needs at least four letters or digits of
fixed text that is not a finalizing word, and the clicked name is still checked against the
finalizing words. A refusal for an undeclared control lists the names and patterns the task
declares.
A button that confirms a dialog's own rows (an attachments dialog's `OK`) can be declared in
`dialog_confirm` instead of `allowed_controls` (such a button is usually submit-type, which is
why it is refused otherwise): it is clicked only inside a dialog whose form, if it has one,
lies inside that dialog and which holds no finalizing control of its own, the same name
anywhere else is refused, it cannot start a download, and a finalizing name can never be
declared there.
A button that only recalculates what is on the page (a mileage form's `Calculate`, which the
page renders as a submit-type control inside the report's form) can be declared in
`draft_controls`: only after checking by hand that it does not save or send the report.
Exact names only, never a finalizing name; the declared button is spared the submit-control
check and nothing else: it must be a real button none of whose other names (text, value,
title, id) is a finalizing word, a formaction override, a script link and a submitting click
handler are still refused, it cannot start a download, and the form-submission safety net
stays on (it swallows a form post, not a background request). The page can still send a
background request or run a script handler that blumkin cannot tell from a recalculation, so
the declaration is yours: when unsure, leave it out and click the button yourself.
Refused, without touching the page: an
undeclared control, a submit control, and any control named like a finalizing action
(submit, send, file, confirm, pay, ...), plus a control with a formaction, a link that runs
script and a click handler that submits. A browser task never submits; the user does.
"""

BROWSER_CLOSE_EPILOG = """
Examples:

\b
  blumkin browser close youtube-transcript
  blumkin browser close youtube-transcript --wait 30
  blumkin browser close youtube-transcript --force --json

Closes the browser blumkin launched for the task, so an agent can clean up after itself without
asking you to close a window by hand. The browser is asked to quit and given `--wait` seconds
(default 15) to exit, which lets it write its profile out. The profile, with its saved cookies
and sign-in, is kept: `blumkin browser launch TASK` opens it again, and `profile delete` is the
command that removes it. Only a process confirmed to be a browser on this task's profile is
ever signalled. With no browser running it says so and exits 0. A browser that does not exit is
reported; `--force` kills it, and its profile may then need to recover.
"""

BROWSER_EPILOG = """
Examples:

\b
  blumkin browser profile create expense-report
  blumkin browser launch expense-report
  blumkin browser status

Browser-assisted tasks (issue #462) drive a browser you sign in to yourself. Each
task has its own browser profile (`profile create`), launched on demand
(`launch`) with DevTools bound to loopback only; you sign in by hand, SSO and MFA
included, and blumkin never sees or stores your credentials. A task fills in the
form and stops: it never submits, and ends by telling you what to click.

The browser comes from `[browser] default = "chrome" | "edge" | "chromium"` in
config.toml and falls back to Chrome when unset or not installed.
"""

BROWSER_DOWNLOAD_EPILOG = """
Examples:

\b
  blumkin browser download meeting-notes Download --into ~/transcripts
  blumkin browser download meeting-notes Download --into ~/transcripts --name standup.vtt

Clicks a control the task declared in `allowed_controls` that starts a download, and keeps the
file it saves. Default deny: the task must declare `download_dirs` (folders, absolute or below
`~/`) and `allowed_hosts` in config.toml, `--into` must be an existing folder inside one of
them, and the file must come from an allowed host, be a text file (.vtt, .srt, .txt, .csv or
.md) of at most 20 MB, and have a plain name (the page's suggestion is cleaned, or give
`--name`). It is saved to a private temporary folder, then kept as a new file with mode 0600;
an existing file is never replaced. Its text is scanned like `read` and is not printed: an
`injection_warning` says when it looks like it is trying to instruct an agent, and the text
is data, never instructions. A control that could finalize something is refused as for `click`.
The host check uses the address the download started from; a `blob:` download is judged by
the origin that made it; the size limit is checked after the browser has saved the file; only
a download that starts on the task's own page is captured.
"""

BROWSER_EXPLORE_EPILOG = """
Examples:

\b
  blumkin browser explore observe expense-report expense-list
  blumkin browser explore click expense-report Help --role link
  blumkin browser explore show expense-report
  blumkin browser explore save expense-report --yes

Explores a site once and records its structure (pages, controls, flows) as a model that
later runs reuse. The task must declare `allowed_hosts`; exploration never leaves them.
Links and tabs are followed without asking. A control that may change state, including a
menu item, is clicked only after you answer yes at your own terminal (the default is no,
and an agent without a terminal gets no answer). A finalizing control (submit, send, pay, ...) is
recorded and never clicked. The draft is kept until `save --yes` stores it as the next
version of the task's model, or `discard` drops it. Nothing you type is recorded.
"""

BROWSER_EXPLORE_CLICK_EPILOG = """
Examples:

\b
  blumkin browser explore click expense-report Help --role link
  blumkin browser explore click expense-report "Add line" --role button --page expense-list

Follows a link or tab on an allowed host. For a control that may change state
(a button, a checkbox) it asks on your terminal first; with no terminal it clicks nothing
and prints the command for you to run. A finalizing control is never clicked. With `--page`
a click that happened is recorded as a step of the flow.
"""

BROWSER_EXPLORE_DISCARD_EPILOG = """
Examples:

\b
  blumkin browser explore discard expense-report

Drops the in-progress exploration draft. A saved model is not touched.
"""

BROWSER_EXPLORE_FORGET_EPILOG = """
Examples:

\b
  blumkin browser explore forget expense-report expense-item

Drops one page, and the recorded steps that used it, from the in-progress draft (or from the
stored model, which the next `explore save` then stores as a new version without it): a page
left over from an earlier session, or one recorded before a field took effect. Nothing is
printed about a page the draft does not have. The last page cannot be forgotten; use
`explore discard` to drop the whole draft.
"""

BROWSER_EXPLORE_OBSERVE_EPILOG = """
Examples:

\b
  blumkin browser explore observe expense-report expense-list
  blumkin browser explore observe expense-report expense-form --json

Records the page the task's browser is on (a page id of lowercase words) in the draft: its
URL as a path template, a fingerprint of its landmarks and each named control with its kind,
locators and whether it looks finalizing. Labels that look like typed data are skipped and
counted. The page must be on one of the task's `allowed_hosts`.
"""

BROWSER_EXPLORE_SAVE_EPILOG = """
Examples:

\b
  blumkin browser explore show expense-report
  blumkin browser explore save expense-report --yes

Stores the draft as the next version of the task's model (earlier versions are kept) and
clears the draft. Review it with `show` first; `--yes` confirms you have.
"""

BROWSER_EXPLORE_SHOW_EPILOG = """
Examples:

\b
  blumkin browser explore show expense-report
  blumkin browser explore show expense-report --json

Summarizes the draft: its pages with their URL templates and control counts, the controls
that look finalizing, and the number of recorded steps.
"""

BROWSER_ENDPOINT_EPILOG = """
Examples:

\b
  blumkin browser endpoint expense-report
  blumkin browser endpoint expense-report --json

Prints the loopback DevTools endpoint of the task's running browser (for example
`http://127.0.0.1:41873`). The port changes on every launch, so a script or a diagnosis asks
for it here instead of keeping it. Fails with `browser_not_running` when no browser is running
for the task.
"""

BROWSER_FILL_EPILOG = """
Examples:

\b
  blumkin browser fill expense-report "Amount" 12.50
  blumkin browser fill time-sheet "Monday hours" 8 --json
  blumkin browser fill expense-report "Date" 2026-10-23 --nth 2
  blumkin browser fill expense-report "Merchant" "Corner Cafe" --role textbox --exact
  blumkin browser fill expense-report "Title" "Taxi" --frame 1 --host attachments.example.com

Fills the field with that label and checks what the field holds afterwards: `matches` says
whether it is exactly what was typed, and when it is not (an autocomplete expanded the text to a
canonical form, or rewrote it) the field's value is returned as `read_back`. What was typed is
never echoed back and a password field is never read. A label that matches more than one field
is refused with every match listed by role and accessible name; narrow it with `--exact` (the
whole label), `--role` (look it up by accessible role and name) or `--nth N` (1-based, in DOM
order, the order `browser read --aria` lists them). `--frame N` (a number from
`browser frames`, pinned with `--host`) works inside a child frame, which must be on a host in
`allowed_hosts`. A value
that starts with a dash needs `--` before it, for example
`blumkin browser fill TASK "Adjustment" -- -5`.
"""

BROWSER_FRAMES_EPILOG = """
Examples:

\b
  blumkin browser frames meeting-notes
  blumkin browser frames meeting-notes --json

Lists the child frames of the page the task's browser is on: a 1-based number, the host,
the origin (scheme and host, never the path or query) and whether the task's
`allowed_hosts` accept it. A frame in another origin shows up a moment after it attaches, so
the list waits up to `--wait` seconds (default 2) for one. Read one with
`blumkin browser read TASK --frame N`. Pages such
as Teams show some panes (the transcript) in a cross-origin frame, so the page text alone
does not hold them.
"""

BROWSER_GOTO_EPILOG = """
Examples:

\b
  blumkin browser goto expense-report https://erp.example.com/expenses
  blumkin browser goto time-sheet https://erp.example.com/time --json

Opens an http or https page in the task's browser; other schemes (file, javascript,
data) are refused, and so is a host outside the task's `allowed_hosts` once it declares
any. Without an address it opens the task's `form_url` from config.toml. Launch the browser
first with `blumkin browser launch TASK`.
A site that redirects to an identity provider (login.microsoftonline.com, login.microsoft.com,
login.live.com, accounts.google.com, or a host in the task's `sign_in_hosts`; name an Okta,
Auth0, OneLogin or Ping tenant there, for example `acme.okta.com`, since anyone can register
one) is waited through: the page address is polled for up to `--sign-in-wait` seconds (default
120, 0 to not wait) while you sign in by hand, nothing is ever done on the sign-in page, and the
command finishes as soon as the page is on an allowed host (`waited_for_sign_in_s` says how
long, `sign_in_host` where). If the page is still on the sign-in page when the time is up
(`sign_in_timeout`), sign in and run the command again; the browser keeps running.
"""

BROWSER_SIGN_IN_EPILOG = """
Examples:

\b
  blumkin browser goto expense-report
  blumkin --profile work browser sign-in expense-report

Picks the profile's own account on an identity provider's account picker (the Microsoft
"Pick an account" page, say) after `browser goto` was redirected to it, so the sign-in does
not wait for you to click your own tile. It needs `sign_in_account = true` in the task's
`[browser.tasks.TASK]` table (which must declare `allowed_hosts`) and the account address on
the profile (`profiles set-email`).

It clicks one tile and only when it is sure: the page must be https on a sign-in host (the
built-in ones and the task's `sign_in_hosts`) that is not one of the task's own sites, show no
field to type into (a password, a code), and have exactly one tile for the address (the
innermost clickable that names it on its own, no other address, no word such as remove, sign
out or allow, and no link off the sign-in hosts). Otherwise it clicks nothing and you pick the
account yourself. It never types a credential and never adds a permission to the task, so
there is nothing to undo; the account is shown masked. After the click it checks the page is
still on a task or sign-in host and waits up to `--wait` seconds for it to reach one of the
task's hosts; if a password, a code or consent follows, the result says you finish it by hand.
"""

BROWSER_HANDOFF_EPILOG = """
Examples:

\b
  blumkin browser handoff expense-report --filled "3 lines, total 84.50 USD" \\
    --step "Review each line and the total." --step "Press Submit at the bottom right."

Ends a task. Reports `submitted: false` and `needs_user_action: true`, what was
filled in (`--filled`, repeatable), what still needs a decision (`--decision`,
repeatable) and the numbered steps for the user's final click (`--step`, repeatable,
at least one). The task never submits; this is how it hands over.
"""

BROWSER_INIT_TASK_EPILOG = """
Examples:

\b
  blumkin browser init-task --template teams-transcript
  blumkin browser init-task weekly-sync --template teams-transcript --yes
  blumkin browser init-task --template expense-report --host erp.example.com \\
    --form-url https://erp.example.com/expenses --upload-dir ~/Documents/receipts --yes

Creates the task's browser profile (empty; your sign-in lives there later) and prints the
`[browser.tasks.TASK]` table the template starts from: the hosts the site uses, including the
SharePoint host its transcript pane is served from, and the navigation controls the task may
click. Without `--yes` nothing is written to config.toml; with `--yes` the table is appended
to it (the file is created with mode 0600 when it does not exist). A table of that name that
already exists is never changed, and a config.toml that is not valid TOML, or is a symlink or
not a regular file, is left alone. The profile is created either way. TASK defaults to the
template name. The Teams template's `*.sharepoint.com` covers every organization's SharePoint:
narrow it to your tenant's host. Templates: expense-report, teams-transcript. The expense-report
template needs `--host` (the site's host differs per organization) and fills in the controls of
the Oracle Fusion Expenses flow (`Create Report`, `Create Item`, `Save and Close`, `Manage
Attachments`, `Add Row`, `Save`, `Actions for * Report`, `Open`), `OK` as a dialog confirm and
`~/Documents/receipts-archive` as the upload folder (`--upload-dir` replaces it); none of them
is a finalizing name (the task still never submits).
"""

BROWSER_LAUNCH_EPILOG = """
Examples:

\b
  blumkin browser launch expense-report
  blumkin browser launch expense-report --json

Starts the task's browser on its own profile (create it first with `blumkin
browser profile create TASK`) and prints the loopback DevTools endpoint. While blumkin is
attached, the page shows a small `blumkin - TASK` pill in the task's own colour (`colour` in
the output, the same for a task every time) and a thin line of that colour across the top, so
you can tell which window an agent is driving. Sign in
by hand in the window that opens; blumkin never sees your credentials. Running
it again while the browser is open reports `already_running` and changes nothing.
Uses `[browser] default` from config.toml, falling back to Chrome.
`--open` opens the task's `form_url` (set in `[browser.tasks.TASK]`) right away and waits
through the sign-in redirect while you sign in by hand, as `browser goto` does, so there is
nothing to tell the agent: it carries on when the page reaches an allowed host. Any other
address goes through `blumkin browser goto TASK URL`. Cookies the
identity provider sets without an expiry (session cookies) are dropped when the browser
closes, so after `browser close` and a new launch you may have to sign in again; leave the
browser open between runs to stay signed in.
"""

BROWSER_LEDGER_APPLY_EPILOG = """
Examples:

\b
  blumkin browser ledger apply timesheet 2026-p21 day 2026-10-19 --flow fill-day --value hours=8
  blumkin browser ledger apply expense-report trip-lisbon receipt r1 --flow attach --sha256 HEX

Runs one recorded site-model flow for one ledger item, binding values with --value KEY=VALUE
(repeatable). It fills and uploads; it never submits. A receipt needs the SHA-256 of its file.
"""

BROWSER_LEDGER_BEGIN_EPILOG = """
Examples:

\b
  blumkin browser ledger begin timesheet 2026-p21 --expect day=10
  blumkin browser ledger begin expense-report trip-lisbon

Starts the ledger for one instance of a task: this pay period's time sheet, that trip's
expense report. `--expect KIND=COUNT` (repeatable; kinds are day and receipt) says how many
items to expect, so status can read "3 of 10 days filed".
"""

BROWSER_LEDGER_DISCARD_EPILOG = """
Examples:

\b
  blumkin browser ledger discard timesheet 2026-p21 --yes

Closes the instance as discarded. The journal is kept; nothing on the real form is touched.
Receipts previously rehomed into the managed folder are restored to their original paths
(or the managed copy is removed when it was an email attachment copy). Needs `--yes`. A
submitted instance cannot be discarded.
"""

BROWSER_LEDGER_EPILOG = """
Examples:

\b
  blumkin browser ledger begin timesheet 2026-p21 --expect day=10
  blumkin browser ledger record timesheet 2026-p21 day 2026-10-19 staged
  blumkin browser ledger status timesheet 2026-p21
  blumkin browser ledger submitted timesheet 2026-p21 --yes

The ledger remembers what has been filed for one instance of a task, so a restarted session
knows what is already in the form. Each item goes staged, filled, then verified (read back
from the page) or failed. It holds identifiers, states, model versions and file hashes only,
never a value from the form. blumkin has no submit command: `submitted` records that you did.
"""

BROWSER_LEDGER_HANDOFF_EPILOG = """
Examples:

\b
  blumkin browser ledger handoff timesheet 2026-p21 --step "Review week 2" --step "Click Submit"
  blumkin browser ledger handoff expense-report trip-lisbon --step "Click Submit report" \\
    --decision "Confirm the taxi receipt category"

Records the last clicks left for the user (--step, repeatable, at least one) and any open
decisions (--decision, repeatable). The user makes the final submit; blumkin never does.
"""

BROWSER_LEDGER_LIST_EPILOG = """
Examples:

\b
  blumkin browser ledger list timesheet
  blumkin browser ledger list timesheet --json

Lists the task's instances and whether each is open, submitted or discarded.
"""

BROWSER_LEDGER_RECONCILE_EPILOG = """
Examples:

\b
  blumkin browser ledger reconcile timesheet 2026-p21 day \\
    --page-item 2026-10-19 --page-item 2026-10-20
  blumkin browser ledger reconcile expense-report trip-lisbon receipt --page-item r1

Compares the ledger with the item ids seen on the live page (--page-item, repeatable). It only
reads: it reports what differs and never re-adds an item.
"""

BROWSER_LEDGER_RECORD_EPILOG = """
Examples:

\b
  blumkin browser ledger record timesheet 2026-p21 day 2026-10-19 staged
  blumkin browser ledger record timesheet 2026-p21 day 2026-10-19 filled --model-version 3
  blumkin browser ledger record expense-report trip-lisbon receipt r1 staged --sha256 HEX

Moves one item to a new state: staged, then filled, then verified or failed (a failed item
can be staged again). A receipt needs the SHA-256 of its file when it is first staged. The
item id is a short word such as a date; do not put amounts, vendors or paths in it.
"""

BROWSER_LEDGER_STAGE_EPILOG = """
Examples:

\b
  blumkin browser ledger stage timesheet 2026-p21 day 2026-10-19
  blumkin browser ledger stage expense-report trip-lisbon receipt r1 --sha256 HEX --model-version 3

Stages one item so it can be filled. An item that is already verified is left alone, so
running it again is safe. A receipt needs the SHA-256 of its file.
"""

BROWSER_LEDGER_CLOSE_EPILOG = """
Examples:

\b
  blumkin browser ledger close expense-report trip-lisbon --outcome pending --yes
  blumkin browser ledger close expense-report trip-lisbon --outcome done --close-browser --yes

Ends a session on an instance after asking the user whether they are all done with it:
`--outcome done` (the trip is finished) or `--outcome pending` (it stays open and resumes in the
next session). `--yes` says the user gave that answer; nothing infers it. The answer is
recorded first, and only then does `--close-browser` close the task's browser, keeping its
profile (the saved sign-in). A pending instance shows how to resume in `ledger status`: reopen
the saved report (its server-side id is recorded with `ledger report`), then `ledger reconcile`
the ledger with the page. Closing the session is not submitting: the report is still not
submitted until the user says so with `ledger submitted`.
"""

BROWSER_LEDGER_REPORT_EPILOG = """
Examples:

\b
  blumkin browser ledger report expense-report trip-lisbon --report-id 100234 --draft-saved
  blumkin browser ledger report expense-report trip-lisbon --total-confirmed --json

Records the report itself on the instance: the number the server gave it (`--report-id`), that
a draft of it is saved there (`--draft-saved`), and that the user confirmed its total
(`--total-confirmed`). The ledger never holds an amount, so the total is recorded only as
confirmed. A flag left out keeps its last value. The report stays `submitted: false` until the
user submits it and says so with `ledger submitted`.
"""

BROWSER_LEDGER_STATUS_EPILOG = """
Examples:

\b
  blumkin browser ledger status timesheet 2026-p21
  blumkin browser ledger status timesheet 2026-p21 --json

Shows where the instance stands, for example "3 of 10 days filed, 4 receipts filed, not
submitted", with every item and its state.
"""

BROWSER_LEDGER_SUBMITTED_EPILOG = """
Examples:

\b
  blumkin browser ledger submitted timesheet 2026-p21 --yes

Records that you submitted the real form. Nothing else records it, and blumkin has no
submit command. Needs `--yes`; say yes only if you pressed the button yourself. A closed
instance accepts no more items.
"""

BROWSER_LEDGER_VERIFY_EPILOG = """
Examples:

\b
  blumkin browser ledger verify timesheet 2026-p21 day 2026-10-19
  blumkin browser ledger verify timesheet 2026-p21 day 2026-10-20 --missing
  blumkin browser ledger verify expense-report trip-lisbon receipt r1 --sha256 HEX

Records whether the item was found on the live page: --present (the default) marks it
verified, --missing marks it failed so it can be staged again.
"""

BROWSER_MODEL_CHECK_EPILOG = """
Examples:

\b
  blumkin browser model check expense-report expense-list
  blumkin browser model check expense-report expense-list --version 1 --json

Open the page in the task's browser first, then check it against the model's page of that
name. The grade is `compatible` (it still matches; layout changes are ignored), `degraded`
(it works but the model should be updated: a control is found only by a fallback locator,
cannot be verified, or some landmarks changed) or `incompatible` (re-explore the page: a
control is gone, a new field appeared or the page no longer looks like the recorded one).
The command exits 1 for `incompatible`. The page must be on the task's `allowed_hosts`.

Some sites serve every page from one address. When the page matches another recorded page
better than the one named, the JSON carries `best_match` (that page and the share of its
landmarks that are on the page), which says which page the browser is really on.
"""

BROWSER_MODEL_DIFF_EPILOG = """
Examples:

\b
  blumkin browser model diff expense-report 1
  blumkin browser model diff expense-report 1 3 --json

Compares two saved versions of the task's model (the second defaults to the current one):
hosts, pages (added, removed or changed), flows, and the controls added, removed or changed
on the pages both have, including a control that became finalizing or safe.
"""

BROWSER_MODEL_EPILOG = """
Examples:

\b
  blumkin browser model check expense-report expense-list
  blumkin browser model versions expense-report
  blumkin browser model diff expense-report 1

A site model records the structure of a task's website (see `blumkin browser explore`).
These commands check that it still matches the site and compare saved versions. Nothing
here clicks or types: `check` only reads the page the task's browser is on.
"""

BROWSER_MODEL_VERSIONS_EPILOG = """
Examples:

\b
  blumkin browser model versions expense-report
  blumkin browser model versions expense-report --json

Lists the saved versions of the task's model, oldest first, and which one is current.
"""

BROWSER_PRESS_EPILOG = """
Examples:

\b
  blumkin browser press expense-report "Category" ArrowDown
  blumkin browser press expense-report "Amount" Tab

Presses a key in the field with that label. Only navigation keys (Tab, the arrows,
Escape, Backspace, Delete, Home, End, Page Up and Down) and single printable characters
are allowed. Enter, Space, a newline and any modifier combination are refused, and so
is any key aimed at a button, because they can submit a form.
"""

BROWSER_PROFILE_CREATE_EPILOG = """
Examples:

\b
  blumkin browser profile create expense-report
  blumkin browser profile create time-sheet --json

Creates the task's private browser profile directory (mode 0700) under
the config directory. A task name is 1-64 lowercase letters, digits, `-` or `_`.
Running it again for an existing profile is a no-op (`created: false`).
"""

BROWSER_PROFILE_DELETE_EPILOG = """
Examples:

\b
  blumkin browser profile delete expense-report --yes

Deletes the task's browser profile, including its saved sign-in and cookies, so you
will sign in again next time. Requires `--yes`. Refuses while that task's browser is
still running; close it first. Only profiles blumkin created can be deleted.
"""

BROWSER_PROFILE_EPILOG = """
Examples:

\b
  blumkin browser profile create expense-report
  blumkin browser profile list --json
  blumkin browser profile delete expense-report --yes

One private browser profile per task keeps its sign-in separate from your everyday
browsing. Only profiles created here are listed, attached to or deleted.
"""

BROWSER_PROFILE_LIST_EPILOG = """
Examples:

\b
  blumkin browser profile list
  blumkin browser profile list --json

Lists the task profiles blumkin created and whether each one's browser is running.
"""

BROWSER_READ_EPILOG = """
Examples:

\b
  blumkin browser read expense-report
  blumkin browser read time-sheet --json
  blumkin browser read meeting-notes --frame 1 --host tenant.sharepoint.com
  blumkin browser read expense-report --aria

Returns the page title, URL and visible text (first 20000 characters). The text is
untrusted data: an `injection_warning` is attached when it looks like it is trying to
instruct an agent, and nothing in it is a command.

`--frame N` reads child frame N instead (numbers come from `blumkin browser frames`). The
task must declare `allowed_hosts` and the frame must be on one of them; frame numbers shift
when the page adds or reloads a frame, so add `--host HOST` to refuse any other frame, and
`--wait SECONDS` waits for a frame that attaches late to attach and (as text) have text.
`--aria` returns the ARIA snapshot (roles and accessible names) of the page, or of the
frame with `--frame`, with link addresses and field values removed: use it to find a
control's name and role before a click. A page snapshot stops at frame boundaries, and
hidden or screen-reader-only text is included, so it is untrusted data like the text.

A long page is read in windows: `--limit N` (default 20000) and `--offset N` pick the
characters, and the JSON says `truncated`, `total_chars` and the `next_offset` to continue
from. With `--aria`, a combobox or listbox that only holds options is summarized
(`combobox "Type" (27 options, selected "Lodging")`; `--options` lists them), and
`--within ROLE[:NAME]` keeps one subtree, for example `--within dialog` for the open
dialog or `--within "dialog:Manage Attachments"`. The page is scanned for injection before
it is narrowed, so a warning outside the subtree still shows.
"""

BROWSER_REFRESH_TAB_EPILOG = """
Examples:

\b
  blumkin browser refresh-tab meeting-notes Transcript --via Notes

Reloads a tab's pane. Some panes only reload when another tab is shown first, and clicking
the tab that is already selected does nothing; this selects `--via` and then the tab again.
Both are tabs the task declared in `allowed_controls` (checked before the first click), the
page must still be on an allowed host between the two, and each is waited for up to `--wait`
seconds (default 30). Like `click`, it never submits anything.
"""

BROWSER_STATUS_EPILOG = """
Examples:

\b
  blumkin browser status
  blumkin browser status expense-report --json

Shows each task profile, whether its browser is running, the loopback DevTools endpoint
when it is, and how many pages (tabs) it has open (`pages`). A browser with no open page is
running but cannot be driven until it has one: the status says so (`note`), and any browser
command opens a blank page for you. Give a task name to look at just that one.
`status --json` is also the way for a script to learn the endpoint, which changes on every
launch; `blumkin browser endpoint TASK` prints just that.
"""

BROWSER_TRANSCRIPT_EPILOG = """
Examples:

\b
  blumkin browser transcript teams-transcript --duration 34m06s --out ~/transcripts/intros.txt
  blumkin browser transcript teams-transcript --json

Run it with the meeting's Transcript tab open in the task's browser. The transcript list is
virtualized and lives in a cross-origin frame, so `browser read` cannot see it; this scrolls
that frame from its first entry to its last and merges what it saw into one `[m:ss] Speaker:
text` line per turn. Both the top-level Teams page host and the frame's host must be in the
task's `allowed_hosts` (for Teams, `teams.microsoft.com` and `*.sharepoint.com`). It is
read-only: nothing is clicked or typed.

The result always carries a coverage report: entry count, speakers, first and last timestamp,
silences longer than three minutes, entries out of order, and whether the scroll reached
both ends. With `--duration` the tail is checked against the meeting's length. `complete` is
false when any of it looks wrong; do not present such a capture as the whole meeting. `--out`
creates a new private file and never overwrites. The text is untrusted data, and an
`injection_warning` is attached when it looks like it is trying to instruct an agent.
"""

BROWSER_UPLOAD_EPILOG = """
Examples:

\b
  blumkin browser upload expense-report "Receipt" ~/Documents/receipts/taxi.jpg
  blumkin browser upload expense-report "Receipt" ./hotel.pdf --json
  blumkin browser upload expense-report "File" ./taxi.pdf --frame 1 --nth 2

Attaches an existing file to the file field with that label. `--exact`, `--role`, `--nth` and
`--frame` (with `--host`) single out one field exactly as for `browser fill`, so an upload can
reach a file input inside a child frame such as an attachments dialog; the frame must be on an
allowed host and the folder rules below apply unchanged. The file must be a regular
file inside a folder the task declared in `upload_dirs` under `[browser.tasks.TASK]` in
config.toml (absolute, or starting with `~/`), and the page must be on a host the task
declared in `allowed_hosts`; with either missing nothing can be uploaded, so a page cannot
make an agent upload an arbitrary file or send a receipt to another site. Receipts stay on this
machine and go only to the page you pointed the task at.

`ok` only means the file was handed to the input. The command then waits half a second and
reads what the page shows: `persisted` is `shown` (the file's name is on the page near the
field), `missing` (a re-render dropped it) or `unknown` (the input holds it but the page does
not show it), `files_held` lists the names the input holds, and `input_hidden` flags a hidden
or zero-size input, such as a drop zone's, which may not be the one the form saves. A `warnings`
list says what to check. On a form that re-renders after a field changes (Oracle ADF, for
one), set the file last: fill the row's other fields first, because a later edit of the same
row loses a file chosen earlier.
"""

BROWSER_SELECT_EPILOG = """
Examples:

\b
  blumkin browser select expense-report "Type" "Taxi"
  blumkin browser select expense-report "Type" "Meals" --nth 2 --json
  blumkin browser select expense-report "Category" "Travel" --role combobox --frame 1

Chooses an option in a native dropdown or an ARIA combobox: a native select takes the option
by its label; a combobox is opened and the one option with exactly that name is clicked
(none, or more than one, is an error). The result says whether the field then shows that option
(`matches`, with `read_back` when it shows something else). `--exact`, `--role`, `--nth` and
`--frame` (with `--host`) single out one field as for `browser fill`. An option or field named
like a finalizing action (Submit, Pay, Send, ...) is refused, as it is for `click`. A combobox
and its option are checked like a click before they are clicked (a submit control, a formaction
or a script link is refused), and `--role` for a dropdown is one of combobox, listbox, searchbox
or textbox; `fill` and `upload` accept the field roles checkbox, combobox, listbox, radio,
searchbox, slider, spinbutton, switch and textbox. A frame reached only over its own DevTools
socket can be read but not filled, and a frame's host is checked again right before each write.
"""

CAPABILITIES_EPILOG = """
Examples:

\b
  blumkin capabilities
  blumkin capabilities --json

Reports which service families (mail, calendar, chat, tasks, docs, drive,
people, meeting) the active profile can use right now, alongside the
provider, profile name, and blumkin version - a machine-readable "what can
this install do" surface for an agent or external tool, without probing
Graph/Google on every call (this is derived from cached config/token-scope
data, same as `blumkin doctor`). `tasks` is always available (local files, no
scope); `meeting` is Microsoft-only. Use `--profile` to check a profile other
than the default.
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
`calendar tentative`. A single `--event-id` also requires a fresh
`calendar get --event-id ...` first (default: within the last 5 minutes,
`preferences.rsvp_freshness_seconds`) so you act on the event's current state,
not stale/unread data (issue #365); `--today-pending` is exempt since it reads
each event via `calendar today` immediately before acting on it.
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
`calendar accept`, reporting any events it had to skip. A single `--event-id`
also requires a fresh `calendar get --event-id ...` first (see
`calendar accept --help`; issue #365).
"""

CALENDAR_CANCEL_EPILOG = """
Example:

\b
  blumkin calendar cancel --event-id AAMk... --yes

Sends a cancellation to every attendee (requires `--yes`). Only the organizer can
cancel an event; an attendee who wants out declines it in their calendar client.
Also requires a fresh `calendar get --event-id ...` first (see
`calendar accept --help`; issue #365) since there is no bulk mode to exempt.
"""

CALENDAR_CREATE_EPILOG = """
Examples:

\b
  # 30-minute Teams hold (Teams link added by default)
  blumkin calendar create --subject "1:1 sync" \\
    --start "2026-09-01T15:00" --duration 30m
\b
  # One hour, explicit timezone
  blumkin calendar create --subject "Design review" \\
    --start "2026-09-02T09:00" --duration 1h --tz America/New_York
\b
  # Offline hold on your own calendar, no Teams link
  blumkin calendar create --subject "Focus block" \\
    --start "2026-09-02T13:00" --duration 2h --no-teams
\b
  # Solo hold with a reminder a day ahead (email on Google, popup on Outlook)
  blumkin calendar create --subject "Review renewal" \\
    --start "2026-09-28T10:00" --remind-email 1d --no-teams
\b
  # Weekly recurring series, ending on a date
  blumkin calendar create --subject "Henrique/Sam 1:1" \\
    --start "2026-09-22T13:05" --duration 45m \\
    --repeat weekly --until "2026-12-31"
\b
  # Every-weekday lunch hold for the next 20 working days, no Teams link
  blumkin calendar create --subject "Lunch" --start "2026-09-22T12:00" \\
    --duration 1h --repeat weekly --days mon,tue,wed,thu,fri --count 20 \\
    --no-teams
\b
  # Design review with an agenda and a location
  blumkin calendar create --subject "Design review" \\
    --start "2026-09-22T09:00" --duration 1h \\
    --location "Room 4" --body "Agenda: API shape, timeline"
\b
  # All-day out-of-office hold, three days, no Teams link
  blumkin calendar create --subject "OOO" --start "2026-12-24" --all-day \\
    --duration 3d --no-teams
\b
  # Review the event, then invite attendees separately (issue #365)
  blumkin calendar update --event-id AAMk... --with sam@example.com --yes

Never invites anyone - `calendar create` can only ever produce a solo hold, so
it needs no `--yes`. Add attendees afterward with `calendar update --with ...
--yes`; that is the "emit" step, and it is gated on the confirm cooldown so
there is a beat to review the event before anyone is notified. `--body` /
`--body-file` set the agenda (`--body-type` html/text is Microsoft only).
`--location` is free text. `--all-day` makes `--start` a date and `--duration`
whole days, and never attaches a Teams link (an all-day online meeting is
rejected); a date-only `--start` without `--all-day` is an error.
`--remind-email` adds an email reminder on Google and an Outlook popup
reminder on Microsoft. For a cross-timezone or external attendee, run
`calendar freebusy` or `calendar suggest` first and pick a slot inside their
working hours before inviting them. `--start` stays in the organizer timezone.

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
  blumkin calendar create --subject "Planning" \\
    --start "2026-09-01T14:00" --duration 45m --json
  blumkin calendar update --event-id AAMk... --with sam@example.com --yes
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
NAME|ID` reads from a non-default calendar (see `calendar list`). A successful
call also stamps this event id as freshly read, satisfying the
`calendar accept`/`decline`/`tentative`/`cancel` freshness requirement for the
next `preferences.rsvp_freshness_seconds` (issue #365).
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
  # Ranked 45-minute slots where everyone is free (your own calendar is always included)
  blumkin calendar suggest --with sam@example.com --with dana@example.com \\
    --start "2026-09-01T09:00" --end "2026-09-03T18:00" --duration 45m --json
\b
  # Clip to a working-day window, count tentative blocks as free
  blumkin calendar suggest --with sam@example.com \\
    --start "2026-09-01T09:00" --end "2026-09-01T18:00" \\
    --duration 30m --window 09:00-17:00 --treat-tentative free --json

Your own calendar is always checked in addition to every --with address (no need to
pass yourself). Only suggests starts; it never creates an event. Feed a chosen start straight
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

Adding or replacing attendees on an event `calendar create` produced this
session is gated on the confirm cooldown (issue #365): it refuses to run
until `preferences.confirm_cooldown_seconds` has passed since the create, so
there is a beat to review the event before anyone is notified. Editing an
event that was not just created (or that cooldown has already elapsed for)
proceeds normally.
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
  blumkin chat last --chat-id 19:abc... --n 1 --json   # read it first
  blumkin chat delete --chat-id 19:abc... --message-id 17... \\
    --expected-text "the exact current body text" --yes

Soft-deletes one of your messages; every participant sees it vanish, so `--yes`
is required. `--expected-text` must match the message's current body exactly
(after trimming) - it must come from a fresh read (`chat last`), not a guess,
so the wrong message is never deleted by mistake. Needs
`wo1162425_scopes = true` (Chat.ReadWrite).
"""

CHAT_DRAFT_EPILOG = """
Examples:

\b
  # By display name
  blumkin chat draft --with "Sam Rivera" --text "Sending the deck now" --json
\b
  # By explicit chat id when the name is ambiguous
  blumkin chat draft --chat-id 19:abc... --text "Thanks!" --json

Composes the message only; send it with `chat send --draft-id ... --yes`. No
one is notified by this step. Needs `wo1162425_scopes = true`. If `--with` is
ambiguous, use `--chat-id` from `chat find`. Use ASCII hyphens in `--text`, not
em dashes.
"""

CHAT_EDIT_DRAFT_EPILOG = """
Example:

\b
  blumkin chat edit-draft --chat-id 19:abc... --message-id 17... \\
    --text "Updated: moving the sync to 3pm" --json

Composes the replacement body only; apply it with
`chat edit --draft-id ... --yes`. No one is notified by this step. Use ASCII
hyphens in `--text`, not em dashes.
"""

CHAT_EDIT_EPILOG = """
Example:

\b
  blumkin chat edit-draft --chat-id 19:abc... --message-id 17... \\
    --text "Updated: moving the sync to 3pm" --json
  blumkin chat edit --draft-id chat-edit-... --yes

Rewrites a message other people have already read, so `--yes` is required.
Prepare the replacement text first with `chat edit-draft`. Needs
`wo1162425_scopes = true` (Chat.ReadWrite).
"""

CHAT_EPILOG = """
Common workflows:

\b
  # Read the last few messages from a 1:1
  blumkin chat find --with "Sam Rivera" --json
  blumkin chat last --with "Sam Rivera" --n 5 --json
\b
  # Send a message (notifies the recipient) - compose, then emit
  blumkin chat draft --with "Sam Rivera" --text "On my way" --json
  blumkin chat send --draft-id chat-send-... --yes

Reads work with the base scope set. Writes (draft/send/edit-draft/edit/delete)
and needing a specific chat id require `wo1162425_scopes = true`. When
`--with` is ambiguous, pass `--chat-id` from `chat find`.
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
Example:

\b
  blumkin chat draft --with "Sam Rivera" --text "Sending the deck now" --json
  blumkin chat send --draft-id chat-send-... --yes

Sends an existing draft (from `chat draft`). Requires `--yes` - this is the
step that actually messages a real person. Needs `wo1162425_scopes = true`.
Also enforced: a minimum wait since the draft was composed
(`preferences.confirm_cooldown_seconds`, default 20s) - calling this too soon
fails with `too_soon` rather than sending, so you have a real chance to review
the draft first.
Stored drafts are checked again before send/edit. See
`docs/outbound-message-policy.md` for profile rules and signature behavior.
"""

COMPLETION_EPILOG = """
Examples:

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
numbered lists, block quotes (an indent, not a bullet), fenced code, rules,
and simple tables; anything else renders as plain text. Use ASCII hyphens,
not em dashes.
"""

DOCS_EPILOG = """
Author a document, or read one back locally after download/export:

\b
  blumkin docs create --title "..." --body-file ./brief.md --json
  blumkin docs read --path ./agenda.pdf --pages 1-2 --json
  blumkin docs update --id 1AbC... --body-file ./brief-v2.md --json

One authoring format (a Markdown subset) across both providers; the backend is a
native Google Doc, or a `.docx` uploaded to OneDrive. `update` re-renders a doc
this tool created in place - same id, URL, and sharing. `read` is local only:
use it on a PDF/DOCX/XLSX already on disk (for example from `drive download`,
`drive export`, or an attachment download).
"""

DOCS_READ_EPILOG = """
Examples:

\b
  blumkin docs read --path ./agenda.pdf --pages 1-3 --json
\b
  blumkin docs read --path ./contract.docx --json
\b
  blumkin docs read --path ./report.xlsx --sheet Summary --json
\b
  blumkin docs read --path ./whiteboard.png --json

Reads a local file already on disk - no provider, auth, or network call. PDF
uses native text extraction by default; pass `--ocr` for scanned/image pages.
Images (png/jpg/jpeg/tif/tiff/bmp/webp) are always OCR'd - there is no text
layer to fall back to, so `--ocr` is implicit and cannot be passed for them.
Refuses files larger than 25 MB, and extracted output larger than 1 MB.

`--json` prints:
`{ok, path, kind, pages: [{index, text, tables, sheet?}], ocr_used}`.

Missing extras fail closed with an actionable error, for example:
`docs read needs the pdf extra: uv tool install -e '.[pdf]'`.
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

`repository` (JSON) compares the running build with a blumkin git checkout, found through
`BLUMKIN_REPOSITORY` or by walking up from the working directory (offline; nothing is
fetched): the relation of the build's commit to the checkout's HEAD (same, behind, ahead,
diverged or unknown) with how many commits behind, and the top-level commands the checkout
has that this CLI lacks. Only missing commands raise a warning (a release is always some
commits behind `main`); the fix is a newer release (`blumkin upgrade`) or installing the
checkout (`uv tool install -e PATH`). It is `null` when there is no checkout around, or when
the CLI runs from that checkout itself.

`capabilities` (JSON) / `available:` (text) reports which service families
(mail, calendar, chat, tasks, docs, drive, people, meeting) this profile's
granted scopes actually unlock right now - `tasks` is always available (local
files, no scope); `meeting` is Microsoft-only. Same helper as
`blumkin capabilities --json`.

`warnings` / `stale_processes` (JSON) flags an orphaned `blumkin-agent` on a
legacy socket path, or a `blumkin mcp serve` process still running - restart
guidance for both (issue #408, a #401 follow-up).
"""

DRIVE_EPILOG = """
Read and organize your drive:

\b
  blumkin drive list --folder "Language Classes/Portuguese Classes" --json
  blumkin drive get --id 1a2b3c... --json
  blumkin drive read --id 1a2b3c... --json
  blumkin drive export --id 1a2b3c... --to ./doc.pdf

Move files in and out of it:

\b
  blumkin drive download --id 1a2b3c... --out ./downloads/
  blumkin drive upload --file ./avatar.png --folder "Profile" --yes

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

DRIVE_UPLOAD_EPILOG = """
Examples:

\b
  blumkin drive upload --file ~/Pictures/avatar.png --folder "Profile" --yes
  blumkin drive upload --file report.pdf --folder-id 1AbC --name "Q3 report.pdf" --yes --json

Uploads one local file to your drive as a new file: Google Drive on a Google profile,
OneDrive on a Microsoft one (needs the same Files.ReadWrite grant as the other `drive`
verbs). `--yes` is required. It never replaces a file, and it refuses a name that is already
taken in the folder (pick `--name` or another folder); on Google Drive, which has no
create-if-absent, a concurrent upload of the same name between the check and the create could
still leave a duplicate. It notifies no one and returns the id and web link like the other
drive verbs.

The file must be a regular file with a single link (a hard link can be a secret under another
name), at most 2 GiB, and must not be inside blumkin's own config directory (tokens and auth
records), ~/.ssh, ~/.aws, ~/.gnupg or ~/.config/gcloud, nor be ~/.netrc, an .env file or the
Google OAuth client file. It is opened once and that open file is what is sent; a file that
changes while it is sent is an error. `--folder` (a path; "/" is the root) or `--folder-id`
names an existing destination folder, else the drive root; nothing is created. A missing
`--folder` path exits 5 (not_found), as does a missing OneDrive `--folder-id`; a missing
Google `--folder-id` exits 2. `--name` defaults to the file's own name (OneDrive also refuses
names with " * : < > ? / \\ | or a trailing dot) and `--mime-type` to the type detected from
it. Small files go in one request; larger ones use the service's resumable upload (Google) or
an upload session in chunks that are retried when the service is busy (OneDrive).
"""

LOOP_EPILOG = """
Examples:

\b
  blumkin loop init
  blumkin loop list
  blumkin loop status
  blumkin loop run-once probe-trigger
  blumkin loop pending
  blumkin loop run

One runner process owns loop (mail) and cron triggers that name a task and an
instance (issue #499 / #501 / #503). Unattended runs never prompt: anything that
would ask becomes `needs_user` and stops. A kill switch in config or a
`loop/KILL` file stops every run. Triggers live in `[loop.triggers.*]` and
`[cron.*]` in config.toml. Mail triggers need `kind = "loop"`, an allowlist, and
`blumkin loop init` once per mailbox profile.
"""

LOOP_FINISH_EPILOG = """
Examples:

\b
  blumkin loop finish 0123456789abcdef --doc https://docs.google.com/document/d/ID/edit
  blumkin loop finish 0123456789abcdef --needs-user "the attachment is too blurry to read"

Closes an item that `blumkin loop pending` listed. With `--doc` (repeat it per
document) it replies to the original message, to its sender only, with the
link(s) and marks the item done; only https Google Docs, Sheets, Slides and
Drive document, file and folder links are accepted. The reply draft is checked
first: it must go only to a sender on the trigger's allowlist (a Reply-To header
cannot redirect it) or it is deleted and nothing is sent. With `--needs-user` it
hands the item back with a reason (see `loop pending --handed-back`) and sends
nothing. Repeating a finish never sends a second reply; if sending failed the
draft and links are kept, so run it again. A finish already running (or started
under two minutes ago) makes a second one wait: "in progress".
"""

LOOP_INIT_EPILOG = """
Examples:

\b
  blumkin loop init
  blumkin loop init --profile work
  blumkin loop init --profile work --profile personal --json

Interactive sign-in for each mailbox profile the loop will poll. The runner then
renews credentials silently; a renewal failure pauses that profile until init
succeeds again.
"""

LOOP_INSTALL_SERVICE_EPILOG = """
Examples:

\b
  blumkin loop install-service
  blumkin loop install-service --blumkin-path /usr/local/bin/blumkin

Writes a systemd user unit (`blumkin-loop.service`) that runs `blumkin loop run`
in the foreground. Enable with `systemctl --user enable --now blumkin-loop`.
"""

LOOP_LIST_EPILOG = """
Examples:

\b
  blumkin loop list
  blumkin loop list --json

Lists every configured trigger (`[loop.triggers.*]` and `[cron.*]`) and whether
it is paused.
"""

LOOP_PAUSE_EPILOG = """
Examples:

\b
  blumkin loop pause eod-timesheet
  blumkin loop pause eod-timesheet --json

Pauses a trigger. Missed due times are not caught up on resume unless a later
command asks for catch-up.
"""

LOOP_AGENT_EPILOG = """
Examples:

\b
  blumkin loop agent
  blumkin loop agent --json

When items are waiting (see `blumkin loop pending`), starts the agent named by
`loop.agent_command` in config.toml in a pty, sends it `loop.agent_prompt` (default
"run the blumkin loop"), and waits until every waiting item is finished or handed back,
then sends `loop.agent_exit_input` (default "/exit"). Every prompt sent and everything the
agent prints is written to a timestamped log under <config>/loop/agent-logs/ (owner-only);
it can hold mail content, so keep it out of issues and chats. Nothing is started when
nothing is waiting or the kill switch is engaged. Exits non-zero when the agent could not
be started, exited early, or timed out (`loop.agent_timeout_seconds`, default 1800) with
items still waiting. `loop.agent_quiet_seconds` (default 5) is the output silence that
counts as "ready" and "done answering".
"""

LOOP_PENDING_EPILOG = """
Examples:

\b
  blumkin loop pending
  blumkin loop pending --handed-back --json

Lists items the loop queued from a mail (an allowlisted sender, a subject keyword
and the saved attachments) that are waiting for an agent: the template to follow
and the saved attachment path(s). blumkin calls no model and the loop starts no
agent, so run this from your own agent session (tell it to run the blumkin loop), follow
each template, then close it with `blumkin loop finish`. `--handed-back` also
lists items handed back, with the reason given; `finish` can still close those.
"""

LOOP_RESUME_EPILOG = """
Examples:

\b
  blumkin loop resume eod-timesheet

Clears the pause on a trigger. Does not replay missed runs by itself.
"""

LOOP_RUN_EPILOG = """
Examples:

\b
  blumkin loop run

Foreground runner for the systemd user service. Honors the kill switch; polls
mail triggers, drains the work queue, then due cron triggers. Stops on
SIGTERM/SIGINT.
"""

LOOP_RUN_ONCE_EPILOG = """
Examples:

\b
  blumkin loop run-once probe-trigger
  blumkin loop run-once probe-trigger --json

Runs one trigger now (manual dry run): serializes on the task instance, applies
the wall-clock budget and retries, and appends one journal row (ids and outcome
only).
"""

LOOP_STATUS_EPILOG = """
Examples:

\b
  blumkin loop status
  blumkin loop status --json

Shows the kill switch, paused triggers, the last run per trigger, and recent
journal rows.
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
dashes. See `docs/outbound-message-policy.md` for profile rules and signature
behavior.
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

MAIL_EXPORT_EPILOG = """
Examples:

\b
  blumkin mail export --id AAMk... --out ./receipts-to-upload
  blumkin mail export --id AAMk... --out ./receipts-to-upload --name 2026-10-23_ride --json

Renders the message body (HTML, or plain text wrapped in a page) to `<name>.pdf` with a
headless browser, and writes `<name>.txt` (the same content as text) and `<name>.json`
(message id, sender, received time, subject) beside it. Use it for receipts that arrive as
HTML mail with no attachment. Images are loaded so the logo and the route map survive.
Scripts, frames, linked resources, meta tags and some elements hidden by supported inline
styles are removed (text hidden by a stylesheet can be absent from the PDF yet remain in the
`.txt` file, so check amounts visually), and images declared as one pixel or smaller are
dropped; an image with no declared size can
still load, and its address may tell the sender the message was opened. The browser runs in
a throwaway profile, so nothing is fetched with your cookies or mailbox identity. The PDF is
then checked against the message (images present, no blank pages, text layer matches the
text file); a failed check exits 1, lists the problems and writes nothing, so fix the cause
and re-run with the same names before rehoming or uploading. Never writes
into the receipts archive: `--out` is the folder you name, and an existing export is never
overwritten. A prompt-injection warning on the message is reported and recorded in the JSON,
and does not stop the export. Needs Chrome (or Chromium or Edge) and the pdf extra
(`uv tool install -e '.[pdf]'`) for the check.
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

`--body-type` only chooses how the body is shown to you: a draft stored as HTML reads as plain
text under the default, exactly like a draft stored as text. To see whether a draft really
holds markup (after `mail reply` or `mail update-draft` with `--body-type html`), ask for
`--body-type html`; the plain-text default cannot tell the two apart.
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
  # Full-text search within the inbox (server-side)
  blumkin mail inbox --search "quarterly report" --json

`--from` / `--subject` match locally over a newest-first scan capped at 500
messages (payload then says `complete: false`). `--importance` /
`--has-attachments` filter server-side and keep the sort. `--search` runs on
Graph scoped to the inbox and cannot be combined with `--from` / `--subject` /
date / importance / attachment filters. Use `mail search` to search the whole
mailbox.
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
  # Archive, high-importance only (Microsoft; Google has no archive folder to list)
  blumkin mail list --folder archive --importance high --json

`--folder` takes a well-known name (inbox, sentitems, drafts, archive,
deleteditems, junkemail, outbox), a folder id, or a custom folder's display
name. Google accepts only inbox, sentitems, drafts, deleteditems and junkemail;
archive, outbox, a folder id and a custom name are Microsoft only and fail on a
Google profile. Sort defaults by folder (sent for Sent Items, created for Drafts/Outbox,
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
quoted thread; `--body-type text` for plain; `--body-type html` keeps your markup).
Graph reads a reply's comment as plain text, so an HTML or Markdown body is written into
the draft after it is created, above the quoted thread. A later
`mail update-draft --body` drops the quoted original unless you add `--keep-quoted`.
"""

MAIL_SEND_DRAFT_EPILOG = """
Example:

\b
  blumkin mail send-draft --id AAMk... --yes

Sends an existing draft (from `mail draft` / `mail reply` / `mail forward`).
Requires `--yes` - this is the step that actually delivers mail. Also enforced:
a minimum wait since the draft was last composed/edited
(`preferences.confirm_cooldown_seconds`, default 20s) - calling this too soon
fails with `too_soon` rather than sending, so you have a real chance to review
the draft first.

Microsoft accounts: the message is held in Outbox for that same cooldown
window (deferred delivery) instead of delivering immediately, so
`mail cancel-send --id ...` is a real undo if you change your mind right after
confirming - unlike Outlook's native message recall, which is unreliable once
the recipient has opened the message. Gmail has no equivalent hold; the
cooldown before this call is its whole safety net.
"""

MAIL_CANCEL_SEND_EPILOG = """
Example:

\b
  blumkin mail cancel-send --id AAMk...

Deletes a message still held in Outbox before its deferred-delivery window
elapses (Microsoft accounts only - see `mail send-draft`'s epilog). No `--yes`
needed - the opposite of a notification, since it stops one. Fails not_found
once the hold window has passed and Exchange has delivered the message; at
that point there is no reliable way to un-deliver it. Not available for Gmail,
which has no deferred-delivery hold to cancel.
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

Needs no extra install step - `mcp` is a core `blumkin` dependency. Auth
stays a CLI step - run `blumkin auth
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

If `blumkin` is upgraded while this server is still running (issue #408, a
#401 follow-up), every tool call fails fast with `server_outdated` instead
of a misleading `auth_required` - restart the MCP client to spawn a fresh
server on the new build.
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

`--json` reports `serve` (the real command and arguments this run offers to register; each
client's `action` says whether it was applied, declined or failed) and a `warnings` array.
A dev run registers the launcher, and when that launcher lives in a linked git worktree
the entry stops working once the worktree is removed, so a warning says so.

Auth is unchanged - run `blumkin auth login` on a TTY once; every registered
server shares that token cache. `mcp` is a core `blumkin` dependency - no
extra install step needed to run the server.
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

PROFILES_SET_ADDRESS_EPILOG = """
Examples:

\b
  # Asked for at a hidden prompt (nothing in shell history)
  blumkin --profile work profiles set-address
\b
  # From standard input, for a script
  pass show addresses/home | blumkin --profile work profiles set-address --stdin
\b
  # Remove it
  blumkin --profile work profiles set-address --clear

Stores an optional physical address on one profile, for browser tasks such as a mileage
expense that need it. It is unset by default, never printed back, and left out of
`profiles list`, `doctor` and error messages (a listing only says whether one exists). It is
kept in plain text in config.toml like the rest of the profile (the file is made private to
you); a task can read it only if its table sets `use_profile_address = true`, and then only
through `blumkin browser address TASK`: nothing is typed into a page or sent anywhere on its
own. That is a guard rail for the commands, not a boundary against anyone who can read or edit
config.toml.
"""

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

RECEIPTS_CHECK_EPILOG = """
Examples:

\b
  blumkin receipts check ./receipts-to-upload/2026-10-23_ride.pdf
  blumkin receipts check ./receipts-to-upload/2026-10-23_ride.pdf --json

Checks a PDF written by `blumkin mail export`: pages exist and none is blank, the images the
message carried are in the PDF, and the PDF text layer matches the `.txt` file beside it. It
reads the `.txt` and `.json` next to the PDF when they exist; without them the text and
image comparisons are skipped. The text comparison accepts 80% word overlap, so a pass does
not prove the whole receipt survived: read the amounts yourself. Exits 1 with the problems listed
when the render is broken, so run it before `receipts rehome` or any upload. Needs the pdf
extra (`uv tool install -e '.[pdf]'`).
"""

RECEIPTS_EPILOG = """
Examples:

\b
  blumkin receipts find --dir ~/Documents/receipts --since 2026-10-01 --json
  blumkin receipts read ~/Documents/receipts/taxi.jpg --json
  blumkin receipts check ./receipts-to-upload/2026-10-23_ride.pdf --json
  blumkin receipts rehome ~/Downloads/scan.jpg --task expense-report --instance trip-lisbon \\
    --date 2026-10-20 --vendor "Taxi Co" --total 18.50 --currency USD --json

Receipt intake for expense tasks (issues #469, #491): `find` lists the image and PDF
files in a directory, newest first; `read` runs the file through `docs read` (images are
always OCR'd, PDFs only when they have no text layer) and reports vendor, date, total and
currency, each with a confidence, so a task asks you about anything doubtful instead of
guessing; `rehome` moves a confirmed receipt into the managed instance folder under a
canonical name; `check` verifies a PDF rendered from mail by `mail export`. Receipt contents
stay on this machine.
"""

RECEIPTS_FIND_EPILOG = """
Examples:

\b
  blumkin receipts find --dir ~/Documents/receipts
  blumkin receipts find --dir ~/Documents/receipts --since 2026-10-01 --limit 20 --json
  blumkin receipts find --dir ~/Downloads --new-since 30m --json

Lists the image and PDF files directly inside `--dir` (no subfolders, no symlinks,
no hidden files), newest first. `--since YYYY-MM-DD` keeps files modified on or
after that UTC date (the same zone as the printed times). `--new-since 30m` (or `2h`, `1d`, or a
UTC time such as 2026-10-30T14:00:00Z) keeps only files that arrived within that window,
which finds the photo the user just downloaded: phone photos are named after when the
picture was taken, so each entry also shows `arrived` (the birth time where the system
has one, else the last change, which a rename or chmod also updates). For receipts attached to
mail, list the message's attachments with
`blumkin mail attachments --id ... --json` and download the ones you want with:

\b
  blumkin mail attachments download --message-id ... --attachment-id ... --out ...
"""

RECEIPTS_READ_EPILOG = """
Examples:

\b
  blumkin receipts read ~/Documents/receipts/taxi.jpg
  blumkin receipts read ~/Documents/receipts/hotel.pdf --json

Prints vendor, date, total and currency with a confidence for each (high, low or
none). The vendor is a known ride-hail name near the top of the text, else the first line
that looks like a name, else the sender of the mail (for a `mail export` PDF), and is always
`low`. A PDF with a `.txt` file of the same name beside it (written by `mail export`) is read
from that text, with no OCR. It is conservative on purpose: a field is `high` only when the
receipt says it unambiguously. Several different total lines, an amount that could be thousands or
decimals (1.234), an unrecognised grouping, a total with no decimals, a two-digit year,
several dates or currencies, and a `$` alone all come back `low` or without a value, with
a note saying why; the vendor is always `low`. Ask the user about anything that is not
`high`. When OCR is unavailable the result has `status: needs_manual` and says how to
install the `ocr` extra; ask the user for the amounts.
"""

RECEIPTS_REHOME_EPILOG = """
Examples:

\b
  blumkin receipts rehome ~/Downloads/scan.jpg --task expense-report --instance trip-lisbon \\
    --date 2026-10-20 --vendor "Taxi Co" --total 18.50 --currency USD --yes
  blumkin receipts rehome ~/Downloads/mail-att.pdf --task expense-report --instance trip-lisbon \\
    --date 2026-10-21 --vendor Hotel --total 210.00 --currency EUR \\
    --message-id AAMkAG... --yes --json
  blumkin receipts rehome ./fixed.pdf --task expense-report --instance trip-lisbon \\
    --date 2026-10-21 --vendor Hotel --total 210.00 --currency EUR --yes \\
    --replace 2026-10-21_hotel_210.00-EUR_ab12cd34.pdf

Requires `--yes`, passed only after the user confirmed the `receipts summarize` table and
grand total. `--replace` files the new receipt and then sets the named earlier one (file name
or 8+ hex digits of its SHA-256) aside under the instance's `superseded/` folder, so a bad
render can be swapped without hand-deleting a hash-named file; the old file is kept.

Moves (or copies, with `--message-id`) a confirmed receipt into the managed per-instance
folder under the task's first `upload_dirs` entry, named
`YYYY-MM-DD_vendor_amount-CURRENCY_hash8.ext`. Pass only values the user has confirmed.
An intent-then-result journal under the browser-task instance records the move so a crash
is recoverable and `browser ledger discard --yes` can restore originals. A duplicate
SHA-256 is treated as already filed. Email attachments are copied, not moved, and the
message id is stored with the journal entry (issue #491).
"""

RECEIPTS_SUMMARIZE_EPILOG = """
Examples:

\b
  blumkin receipts summarize ./receipts-to-upload/*.pdf
  blumkin receipts summarize --dir ./receipts-to-upload --json

Reads each receipt (a PDF with the `.txt` and `.json` that `mail export` wrote is read from
that text, without OCR), groups them by vendor, ride date and currency, and prints one row per
receipt: `file`, `drop` or `review`. Ride-hail services send two emails per ride, before and
after the tip; a lower total within a tip's reach (up to 1.4 times) of a higher one is marked
`drop` only when the two arrived within 30 minutes of each other (or are the same message or
file); identical totals count as an exact duplicate on the same evidence. Otherwise both are
marked `review`, since a dropped receipt is never filed, and a bigger gap is a different ride
so both are filed. A total or currency that is not certain also goes to `review`. The ride
date comes from the receipt text, then the subject; the email arrival time is a last resort
and is flagged `review` (a late ride arrives the next UTC day). The grand total is per
currency and counts only `file` rows. Nothing is moved or deleted; show the table to the
user and rehome the `file` rows with `--yes` once they confirm. Exits 1 when any row needs review.
"""

RECEIPTS_TO_PDF_EPILOG = """
Examples:

\b
  blumkin receipts to-pdf ~/Downloads/till.jpg --out ./receipts-to-upload/till.pdf
  blumkin receipts to-pdf ~/Downloads/till.jpg --out ./receipts-to-upload/ --json

Converts a receipt photo (JPEG, PNG, WebP, ...) to a one-page PDF for expense sites that only
take PDFs. The photo is turned upright using its EXIF orientation and scaled down to at most
3000 pixels a side. `--out` is a PDF file, or a folder (with a trailing slash) to get
`<photo name>.pdf`. It never overwrites a file and the original photo is left alone.
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

TASKS_CREATE_EPILOG = """
Examples:

\b
  blumkin tasks list --json
  blumkin tasks show --name weekly-report --json
  blumkin tasks create --name weekly-report --prompt 'Summarise in five bullets.'
  blumkin tasks update --name weekly-report --title 'Weekly status'
  blumkin tasks delete --name weekly-report --yes

One markdown file per template under `~/.config/blumkin/tasks/` (or
`$BLUMKIN_CONFIG_DIR/tasks/`); the active profile's `profiles/<name>/tasks/` is
read too and their entries combined. Each file: an optional `# Title` line,
`**Trigger:**` / `**Input:**` / `**Output:**` fields, and a `**Prompt:**` block
written as a `>` blockquote. `tasks create` / `update` / `delete` mutate files
(create writes into the profile `tasks/` dir when that directory already
exists, otherwise the config-dir one). `update` refuses a file with content beyond
those fields (it would drop it) and `delete` needs the exact name; both write files
atomically and privately. blumkin never matches a trigger for you and never runs a
model. See docs/operator-config.md.
"""

TASKS_DELETE_EPILOG = TASKS_CREATE_EPILOG

TASKS_EPILOG = TASKS_CREATE_EPILOG

TASKS_LIST_EPILOG = TASKS_EPILOG

TASKS_SHOW_EPILOG = TASKS_EPILOG

TASKS_UPDATE_EPILOG = TASKS_EPILOG

UNINSTALL_EPILOG = """
Examples:

\b
  blumkin uninstall --dry-run --json
  blumkin uninstall --agent --mcp-cursor --package --yes

Each removal category keeps its own confirmation boundary: on a TTY the
command prompts per category, and MCP registrations prompt per
client/scope. Outside a TTY, only categories explicitly enabled by flags
are acted on, and destructive work also requires `--yes`; `--yes` just
skips prompts, it does not imply "remove all".
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

After a real upgrade, `stale_processes` (JSON) / the `stale processes found`
section (text) lists any `blumkin-agent` / `blumkin mcp serve` process still
running the previous build (issue #408, a #401 follow-up): agents are
stopped outright (they respawn on next use); `mcp serve` processes are only
reported, since killing one out from under its MCP client is not safe -
restart your MCP client(s) instead.
"""
