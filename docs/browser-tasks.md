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

Each command attaches to the running browser, does one thing and detaches; the browser stays open with your session. `blumkin browser click` defaults to a button; for a link, tab or menu item pass the role (`--role link`, `tab` or `menuitem`), for example `blumkin browser click expense-report "Help" --role link`. The two ready-made skills are [`expense-report`](../.agents/skills/expense-report/SKILL.md) and [`time-sheet`](../.agents/skills/time-sheet/SKILL.md).

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

`observe` records the page the task's browser is on in a draft kept on disk between commands, and `show` summarizes what the draft would save. `click` follows links, tabs and menu items without asking. A control that may change state, such as a button that opens a confirmation dialog, is clicked only after you answer yes at your own terminal; so are all menu items and any link whose name or address looks destructive (delete, remove, log out, archive and the like); the default is no, the question is read from the terminal and never from an argument or the page, and with no terminal (an agent running in the background) nothing is clicked and the command prints what to run yourself. An approval covers that one click, is not stored and never adds the control to `allowed_controls`; the element is inspected before you are asked and again at the click, so one that changed in between is refused, and a link that leads off the allowed hosts or out of the web is never followed. While exploring, a navigation to another host is aborted before the request leaves, but a redirect from an allowed host to another one is followed by the browser itself, so that one request is made and the command then fails with `explore_refused`, saying the click happened. A process that opens its own pseudo-terminal could answer the question for you, so approve clicks from your own terminal session. Names in other languages are not recognized as finalizing on links, and a path word such as a user name in a URL is kept in the template, so review the draft with `show` before you save it. A finalizing control (submit, send, pay and so on) is recorded as such and is never clicked, with or without approval. `save --yes` only acknowledges that you reviewed the draft (nothing checks that you did), and stores it as the next version of the task's model under the config directory (`browser-models/`, private, earlier versions kept) and clears the draft; without `--yes` it shows what it would save and stops.

## Receipts

`blumkin receipts find --dir ~/Documents/receipts --json` lists the images and PDFs in a folder, newest first. `blumkin receipts read ~/Documents/receipts/taxi.jpg --json` reads one with the existing OCR and reports vendor, date, total and currency, each with a confidence (`high`, `low` or `none`), so the task asks you about anything doubtful instead of guessing. OCR needs the `ocr` extra (`uv tool install -e '.[pdf,ocr]'`) plus `tesseract` and `poppler`; without it the result says `needs_manual` and the task asks you for the amounts. For receipts that arrive by mail, list a message's attachments with `blumkin mail attachments --id <message-id> --json` and save the ones you want with `blumkin mail attachments download`.

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

## Housekeeping

`blumkin browser status` shows each task profile, whether its browser is running and the loopback DevTools endpoint. `blumkin browser profile list` lists the profiles, and `blumkin browser profile delete expense-report --yes` removes one, saved sign-in and cookies included; it refuses while that task's browser is still open. Only profiles blumkin created are listed, attached to or deleted.

## Errors

| Error | Exit | Meaning |
|-------|------|---------|
| `submit_blocked` | 1 | The guard refused a click or key press. Do not retry; hand the final step to the user. |
| `browser_not_running` | 1 | No browser is running for the task. Run `blumkin browser launch TASK` and sign in again. |
| `browser_action_failed` | 1 | A page action failed or timed out. Read the page and check the label or control name. |
| `browser_launch_failed` | 1 | The browser did not open a DevTools port in time. |
| `browser_not_found` | 5 | Neither the configured browser nor Chrome is installed. |
| `not_found` | 5 | No profile exists for the task (create it with `blumkin browser profile create TASK`), or a file or folder the command named is missing. |
| `upload_blocked` | 1 | The file is not a regular file with a single link inside one of the task's `upload_dirs` folders, the page is not on one of its `allowed_hosts`, or the task declares neither. |
| `host_blocked` | 1 | `goto` was asked for a host outside the task's `allowed_hosts`, or a page the task is on or ended up on is outside them. |
| `profile_in_use` | 1 | Raised when the task's browser is running or a launch for it is in progress; close it or wait, then try again. |
| `explore_refused` | 1 | Exploration needs `allowed_hosts` for the task, or the page is (or a click led) outside them. |
| `model_invalid` | 1 | The draft or the stored model was rejected: a field outside the schema, a value that looks like typed data, a symlink or a file that is too large. |
| `usage_error` | 2 | A bad task name, a missing `--yes`, or a handoff with no `--step`. |
