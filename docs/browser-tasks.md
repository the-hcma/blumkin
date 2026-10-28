# Browser-assisted tasks

Some chores have no API: filing an expense report, filling in a weekly time sheet. blumkin handles them by driving a browser **you** sign in to, filling in the form for you and then stopping. blumkin has no submit command, and a browser task ends by telling you exactly what to review and which button to press. The guard blocks the listed submit paths; the Limits section lists the cases it does not block.

## How it works

1. You create a dedicated browser profile for the task and launch it. You sign in by hand, SSO and MFA included. blumkin never sees, types or stores your credentials; the session cookies live only in that profile directory.
2. An agent (or you) runs the task's skill, which attaches to that browser over the Chrome DevTools Protocol (CDP) with [Playwright](https://playwright.dev/python/), reads the page, fills in fields and uploads files.
3. The task ends with a handoff: what was filled in, what still needs your decision, and numbered steps for the final click. The result always says `submitted: false`.

Chrome, Edge and Chromium are supported. blumkin uses the browser already installed on your machine and never downloads one.

## Set up a task

Create the task's profile and tell blumkin which controls it may click.

```bash
blumkin browser profile create expense-report
```

Then add the task's policy to `~/.config/blumkin/config.toml`.

```toml
[browser]
default = "edge"          # chrome | edge | chromium; Chrome is used when unset or not installed

[browser.tasks.expense-report]
allowed_controls = ["Add line", "Create report"]   # exact names the form shows
extra_deny_names = ["release"]                     # optional; adds to the built-in list
upload_dirs = ["~/Documents/receipts"]             # folders `upload` may read from; none = no uploads
allowed_hosts = ["erp.example.com"]                # sites `upload` may send to; also limits `goto`
```

`allowed_controls` is what makes `blumkin browser click` work for the task: a control that is not listed is refused. `extra_deny_names` can only add to the built-in list of finalizing words, never shorten it. `upload_dirs` is where the task may upload files from (absolute folders, or folders starting with `~/`, but never `/`, the whole home directory or a path that climbs out of it; `~user` paths are refused) and `allowed_hosts` is where it may send them (`erp.example.com`, or `*.example.com` for any subdomain, which needs a dot after the star, though public suffixes such as `co.uk` are not checked); with either missing `blumkin browser upload` refuses every file, so a page cannot make an agent upload an arbitrary file such as a key or a token, or send a receipt to another site. Once a task declares hosts, `blumkin browser goto` refuses any other host too and every other command refuses to act on a page outside them; with no `allowed_hosts`, `goto` is not limited at all, so declare them. An unknown key in a task table is an error, so a typo such as `allowed_control` does not silently leave the task with an empty allow list. Everything that belongs to you personally (the form URL, your receipts folder, your categories, your default weekly hours) lives in your own task definition, not in blumkin; see [`operator-config.md`](./operator-config.md).

## Run a task

Launch the browser, sign in by hand in the window that opens, then let the skill drive it.

```bash
blumkin browser launch expense-report
blumkin browser goto expense-report https://erp.example.com/expenses
blumkin browser read expense-report
blumkin browser fill expense-report "Amount" "12.50"
blumkin browser upload expense-report "Receipt" ~/Documents/receipts/taxi.jpg
blumkin browser click expense-report "Add line"
blumkin browser handoff expense-report --filled "1 line, 12.50 USD" --step "Review the line and the total." --step "Press Submit at the bottom right."
```

Each command attaches to the running browser, does one thing and detaches; the browser stays open with your session. `blumkin browser click` finds the control by name; pass `--role` (`--role link`, `tab` or `menuitem`) to choose when the name exists in more than one role, for example `blumkin browser click expense-report "Help" --role link`. The two ready-made skills are [`expense-report`](../.agents/skills/expense-report/SKILL.md) and [`time-sheet`](../.agents/skills/time-sheet/SKILL.md).

`blumkin browser click` without `--role` finds the control by its name across the clickable roles and says which role it found; a name that exists in more than one role, or two controls of one name within a role, is refused until you pass `--role` or make the name unique (a second role appearing after the first poll found a match is not seen). It waits up to `--wait` seconds for the control: 5 by default when finding it by name, so a missing control fails in seconds, and 30 (the action timeout) when `--role` is given; `--wait` is capped at 300. `blumkin browser refresh-tab TASK TAB --via OTHER` reloads a pane that only reloads when another tab is shown first: both tabs must be declared in `allowed_controls` (checked before the first click), it selects `OTHER`, checks the page is still on an allowed host, then selects `TAB` again; if the second step fails the error says the page was left on `OTHER`. `blumkin browser read TASK --frame N --wait SECONDS` waits for a frame that attaches late (and, for text, for it to have text) instead of reading it empty; a read that fails while the frame is still navigating is retried until the wait is over.

### Look inside a page: frames and the ARIA snapshot

Some panes are not in the page text. `blumkin browser frames TASK` lists the page's child frames: a 1-based number, the host, the origin (never the path or query) and whether the task's `allowed_hosts` accept it. `blumkin browser read TASK --frame N` reads that frame's text (untrusted data, like any page text); it needs `allowed_hosts` and refuses a frame on any other host. `blumkin browser read TASK --aria` returns the ARIA snapshot (roles and accessible names) of the page, or of the frame with `--frame N`: use it to learn a control's name and role before declaring it in `allowed_controls`. Link addresses and field values are removed from it; hidden and screen-reader-only text is not, so it is untrusted data, and a page snapshot stops at frame boundaries. A frame in another origin shows up a moment after it attaches, so `browser frames` waits up to `--wait` seconds (default 2) for one; `--wait 0` lists what is there at once. Frame numbers shift when the page adds or reloads a frame, so list them again before reading, and pass `--host HOST` with `--frame` to refuse any other frame.

## Explore a site once

A task can record the structure of its website once, so later runs do not have to explore it again. The record is a site model: the pages (a URL template and a fingerprint of their landmark controls), the controls on each page (a logical name, a kind, whether it looks finalizing and ordered ways to find it) and the flows that chain them. It holds structure only: nothing you type, no cookie and no personal data, and a label that looks like typed data is skipped and counted. Exploration needs the task to declare `allowed_hosts` and never leaves them.

```bash
blumkin browser explore observe expense-report expense-list
blumkin browser explore click expense-report Help --role link
blumkin browser explore click expense-report "Add line" --page expense-list
blumkin browser explore show expense-report
blumkin browser explore save expense-report --yes
blumkin browser explore discard expense-report
```

`observe` records the page the task's browser is on in a draft kept on disk between commands, and `show` summarizes what the draft would save. `click` follows links and tabs without asking. A control that may change state, such as a button that opens a confirmation dialog, is clicked only after you answer yes at your own terminal; so are all menu items and any link whose name or address looks destructive (delete, remove, log out, archive and the like); the default is no, the question is read from the terminal and never from an argument or the page, and with no terminal (an agent running in the background) nothing is clicked and the command prints what to run yourself. An approval covers that one click, is not stored and never adds the control to `allowed_controls`; the element is inspected before you are asked and again at the click, so one that changed in between is refused, and a link that leads off the allowed hosts or out of the web is never followed. While exploring, a navigation to another host is aborted before the request leaves, but a redirect from an allowed host to another one is followed by the browser itself, so that one request is made and the command then fails with `explore_refused`, saying the click happened. A process that opens its own pseudo-terminal could answer the question for you, so approve clicks from your own terminal session. Names in other languages are not recognized as finalizing on links, and a path word such as a user name in a URL is kept in the template, so review the draft with `show` before you save it. A finalizing control (submit, send, pay and so on) is recorded as such and is never clicked, with or without approval. `save --yes` only acknowledges that you reviewed the draft (nothing checks that you did), and stores it as the next version of the task's model under the config directory (`browser-models/`, private, earlier versions kept) and clears the draft; without `--yes` it shows what it would save and stops.

## Check that a site has not changed

Before a task relies on a recorded model, check that the site still matches it. Open the page in the task's browser, then run the check against the model's page of that name.

```bash
blumkin browser model check expense-report expense-list
blumkin browser model versions expense-report
blumkin browser model diff expense-report 1
```

`check` only reads the page (it never clicks or types) and grades it by role and accessible name, so layout, color and ordering changes are ignored. `compatible` means every control is still found and the page is recognizably the recorded one. `degraded` means it still works but the model should be updated: a control is found only by a fallback locator, cannot be verified from a page snapshot (a selector-only control), or some landmarks changed. `incompatible` means re-explore the page: a control is gone, a new field appeared (it may be required) or the page no longer looks like the recorded one; the command exits 1 (the same code as other failures, so use `--json` and read `grade` to tell them apart). A page with fewer than three recorded landmarks is judged by those alone: none of them on the page, or a different address, makes it `incompatible`. A new button or link is not drift. The page must be on the task's `allowed_hosts`, and the check says when the page's address does not look like the recorded one. `versions` lists the saved versions of the model and `diff` shows what changed between two of them (hosts, pages, flows, and the controls added, removed or changed, including a control that became finalizing or safe).

## Remember what has been filed

A time sheet is filled one day at a time and an expense report one receipt at a time, over several sessions. The ledger remembers, for one instance of a task (this pay period's time sheet, that trip's expense report), what has been filed so far, so a restarted session does not file anything twice. `blumkin browser ledger begin TASK INSTANCE --expect day=10` starts it; the instance name is a short word such as `2026-p21` or `trip-lisbon`. Prefer the ledger-backed verbs: `blumkin browser ledger stage` (idempotent if already verified), `blumkin browser ledger apply --flow NAME` (runs a recorded site-model flow then marks filled; a verified item is a no-op), `blumkin browser ledger verify --present` or `--missing` (read-back), `blumkin browser ledger reconcile KIND --page-item …` (read-only divergence vs the page), and `blumkin browser ledger handoff --step …` (always `submitted: false`, built from the ledger). `blumkin browser ledger record TASK INSTANCE KIND ITEM STATE` remains for manual state fixes. A receipt carries `--sha256` from the start. `blumkin browser ledger status TASK INSTANCE` reads like a sentence, for example "3 of 10 days filed, 4 receipts filed, not submitted"; `blumkin browser ledger list TASK` lists the task's instances.

The ledger is a private append-only journal under the config directory (`browser-tasks/<task>/<instance>/journal.jsonl`, mode `0600`). Writers take a lock and append one line at a time, so two commands cannot corrupt it, a half-written last line left by a crash is ignored and cut off by the next write, and a damaged line anywhere else is reported as `ledger_invalid` instead of being guessed at. It holds item ids, states, model versions and file hashes only, never an amount, a vendor, a path or a field value, so keep ids to dates and short words.

blumkin has no submit command. `blumkin browser ledger submitted TASK INSTANCE --yes` records that you did, and nothing else records it; say yes only if you pressed the button yourself. A submitted or discarded instance accepts no more items. `blumkin browser ledger discard TASK INSTANCE --yes` closes an instance without touching the real form and keeps its journal.

## Read a Teams meeting transcript

`blumkin browser transcript TASK` captures the transcript of a Teams meeting that someone else organized, where `blumkin meeting` has no access and the pane's own Download button is disabled. Open the meeting's chat in the task's browser (sign in by hand first) and its Transcript tab, then run it.

```bash
blumkin browser profile create teams-transcript
blumkin browser launch teams-transcript
blumkin browser goto teams-transcript https://erp.example.com/
blumkin browser click teams-transcript Notes --role tab
blumkin browser click teams-transcript Transcript --role tab
blumkin browser transcript teams-transcript --duration 34m06s --out ~/work/meeting-transcripts/intros.txt
```

```toml
[browser.tasks.teams-transcript]
allowed_hosts = ["erp.example.com", "*.example.com"]
allowed_controls = ["Notes", "Transcript"]
```

`blumkin browser read` cannot do this: Teams serves the transcript from the organizer's OneDrive in a cross-origin iframe, and the list is virtualized, so at any moment only the rows near the viewport exist in the page (about 12 of 34 minutes in the meeting that shaped this command). `transcript` scrolls that frame from its first row to its last, merges the overlap and writes one `[m:ss] Speaker: text` line per turn. The task needs `allowed_hosts` that include both the top-level page host and the frame's host (for Teams, `teams.microsoft.com` and `*.sharepoint.com`; the example uses `erp.example.com` and `*.example.com` as stand-ins), plus `allowed_controls` for the tabs it clicks (`"Notes"`, `"Transcript"`); the command is read-only and clicks nothing. `--out` creates a new file with mode `0600` and never overwrites one.

Everything that knows what the Teams page looks like (the scripts that find and scroll the transcript pane, the parser for its text and the started and stopped transcription event lines) lives in one module, `blumkin.browser.teams`, so a change in the page is one fix; the coverage report and the merge of overlapping chunks are page-independent (`blumkin.browser.transcript`).

Every capture carries a coverage report, so a gap is reported instead of silently missing: the entry count and speakers, the first and last timestamp, silences longer than three minutes, entries out of order and whether the scroll reached both ends. With `--duration` the last entry is checked against the meeting's length, unless Teams logged "stopped transcription" (the transcript legitimately ends before a meeting that kept running). `complete` is false when any of that looks wrong, and a capture that is not complete must not be presented as the whole meeting. The text is untrusted data and goes through the injection scan like `read`.

## Receipts

`blumkin receipts find --dir ~/Documents/receipts --json` lists the images and PDFs in a folder, newest first. `blumkin receipts read ~/Documents/receipts/taxi.jpg --json` reads one with the existing OCR and reports vendor, date, total and currency, each with a confidence (`high`, `low` or `none`), so the task asks you about anything doubtful instead of guessing. OCR needs the `ocr` extra (`uv tool install -e '.[pdf,ocr]'`) plus `tesseract` and `poppler`; without it the result says `needs_manual` and the task asks you for the amounts. After you confirm the fields, `blumkin receipts rehome ~/Downloads/scan.jpg --task expense-report --instance trip-lisbon --date 2026-10-20 --vendor "Taxi Co" --total 18.50 --currency USD` moves the file into the managed per-instance folder under the task's first `upload_dirs` entry, named `YYYY-MM-DD_vendor_amount-CURRENCY_hash8.ext` (the vendor appears in that filename). Pass `--message-id` for a mail attachment so the file is copied (not moved) and the message id is recorded. Rehome keeps a separate private intent/result journal at `browser-tasks/<task>/<instance>/receipt-moves.jsonl` (mode `0600`) that stores resolved source and destination paths, the SHA-256, and optional message id so a crash can be resumed and `browser ledger discard --yes` can restore originals (including an intent-only half-finished move). For receipts that arrive by mail, list a message's attachments with `blumkin mail attachments --id <message-id> --json` and save the ones you want with `blumkin mail attachments download`.

## What blumkin refuses

- Submitting. There is no submit command, and the guard is deny-by-default and enforced in code, not in a prompt. `blumkin browser click` refuses, without touching the page: a control the task did not declare by its exact name, a role that is not clickable, a submit control (judged by the browser's own idea of the control's type, so a button with an invalid `type` counts), a control with a `formaction`, a link that runs script and an inline handler whose text mentions submit, and any control with any word in its name that starts with a finalizing word (submit, send, file, confirm, pay, finish, complete, approve, authorize, place order, and more, so "Payments" and "Save and send" are refused). The deny check runs first: a control is refused even when it is listed in `allowed_controls`. The refusal exits non-zero with `submit_blocked`.
- Keys that submit. `blumkin browser press` allows only navigation keys (Tab, arrows, Escape, Backspace, Delete, Home, End, Page Up and Down) and single printable characters; Shift+Tab also works, but Enter, Space, a newline and every other modifier combination are refused, and so is any key aimed at a button.
- A safety net behind all of that. While blumkin is attached it swallows every non-GET page navigation, which is what submitting a form does, so even a script that submits a form on its own gets no navigating POST through. The page stays as it was, your typed values included. The net lives only on blumkin's connection: once the command finishes it is gone, and your own final click is never blocked.
- Uploading anything else, or anywhere else. `blumkin browser upload` takes only a regular file inside one of the task's `upload_dirs`, judged on the fully resolved path (so a symlink or `..` cannot step out), checks the file and the folder before the browser is touched, checks the page host once attached, and refuses unless the page is on one of the task's `allowed_hosts`; a hard link is refused, because it can point at a file outside the folders.
- Anything but `http` and `https` pages: `goto` refuses `file:`, `javascript:` and `data:` URLs.
- Following the page. Text read from a page or a receipt is data, never instructions, and is scanned for prompt-injection patterns.

## Limits

- The guard is a backstop, not a guarantee. A declared control that is not on the deny list is clicked, so declare only controls that cannot finalize. A form submitted by a GET request is a GET navigation, which the net lets through, and a page can submit from a listener added by script, which the inline-handler check does not see.
- The host checks are detective, not preventive, after a page changes: when a `goto`, a click or an upload ends on a host outside `allowed_hosts` the page has already loaded (nothing from it is returned), and the command fails with `host_blocked` so the task stops before anything is typed into or read from it. `goto` is not limited at all when no `allowed_hosts` are declared.
- Use this on a machine you alone use. The browser's DevTools port listens on `127.0.0.1`, which keeps other computers out but not other user accounts on the same machine, and the profile directory's permissions do not protect a TCP port. On a shared host another user could drive the signed-in browser.
- The safety net protects only while a command is attached. A page that schedules a submit to fire after the command has finished (a timer, for example) is outside it; the guard's checks and your declared controls are what limit that, and the user's own final click is deliberately never blocked.
- After the safety net swallows a submit, the rest of that command may wait on the page for a moment; start a new command or finish the task with `handoff`.
- A form that submits through a background request (a script that posts data without navigating) is not a navigation, so the net does not see it. The guard's checks and your declared controls are what stand between a task and such a page, which is why `allowed_controls` is a short list you wrote.
- The drift check compares what a page snapshot shows, so it cannot see a control that became disabled or required, text in another language, or a locator that is only a selector (those are reported as unverified). Links and menu items are often data (mail subjects, list rows), so a missing one only degrades the grade. Two controls with the same role and name on a page count as one. A page cut off at the control cap is not checked for new fields, and one with more landmarks than the model keeps (100) may report a field it did not record as new.

## Housekeeping

`blumkin browser status` shows each task profile, whether its browser is running and the loopback DevTools endpoint. `blumkin browser profile list` lists the profiles, and `blumkin browser profile delete expense-report --yes` removes one, saved sign-in and cookies included; it refuses while that task's browser is still open. Only profiles blumkin created are listed, attached to or deleted.

## Errors

| Error | Exit | Meaning |
|-------|------|---------|
| `submit_blocked` | 1 | The guard refused a click or key press. Do not retry; hand the final step to the user. |
| `browser_not_running` | 1 | No browser is running for the task. Run `blumkin browser launch TASK` and sign in again. |
| `browser_action_failed` | 1 | A page action failed or timed out. Read the page and check the label or control name. |
| `browser_dependency` | 1 | Playwright is missing or broken. Run `uv sync --reinstall-package playwright` in the blumkin clone, `uv tool install -e . --reinstall`, or `uv tool upgrade blumkin --reinstall-package playwright`, then retry. |
| `browser_launch_failed` | 1 | The browser did not open a DevTools port in time. |
| `browser_not_found` | 5 | Neither the configured browser nor Chrome is installed. |
| `not_found` | 5 | No profile exists for the task (create it with `blumkin browser profile create TASK`), or a file or folder the command named is missing. |
| `upload_blocked` | 1 | The file is not a regular file with a single link inside one of the task's `upload_dirs` folders, the page is not on one of its `allowed_hosts`, or the task declares neither. |
| `host_blocked` | 1 | `goto` was asked for a host outside the task's `allowed_hosts`, or a page the task is on or ended up on is outside them. |
| `profile_in_use` | 1 | Raised when the task's browser is running or a launch for it is in progress; close it or wait, then try again. |
| `explore_refused` | 1 | Exploration needs `allowed_hosts` for the task, or the page is (or a click led) outside them. |
| `model_invalid` | 1 | The draft or the stored model was rejected: a field outside the schema, a value that looks like typed data, a symlink or a file that is too large. |
| `reply_failed` | 1 | `blumkin loop finish` could not send its reply; the item's documents are kept, so run it again. |
| `ledger_invalid` | 1 | A task instance journal has a damaged line (not just a torn last one); restore it from a backup or discard the instance. |
| `usage_error` | 2 | A bad task name, a missing `--yes`, or a handoff with no `--step`. |
