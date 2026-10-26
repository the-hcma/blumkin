# Loop and cron (unattended runner)

`blumkin loop` runs configured triggers in the background: mail-driven browser tasks (expense receipts) and scheduled ones (end-of-day time sheet). It polls allowlisted mailboxes, drains a local work queue, fires due cron entries, journals outcomes, and can send short notices when a run needs you or a mailbox profile pauses. It never explores a new site and never submits a form; browser tasks still stop at handoff for your final click. See also [`browser-tasks.md`](./browser-tasks.md) and [`integrations/systemd/README.md`](../integrations/systemd/README.md).

## When to use it

| Mode | Use when |
|------|----------|
| `blumkin loop` (service or `loop run`) | Unattended catch-up: mail receipts into an expense instance, cron time sheet at end of day |
| Manual browser skill | Interactive session: you are at the keyboard, exploring a site, or filing a one-off report |

Agents should prefer the loop for recurring, already-configured tasks. Do not drive `blumkin browser explore` from the loop. Do not call submit paths; the browser guard already blocks them.

## Configure

Add triggers under `[loop]` in `~/.config/blumkin/config.toml`. Example:

```toml
[loop]
# Optional: stop every run without stopping the service process
# kill_switch = false
# kill_switch_file = "loop/KILL"   # relative to the config dir, or an absolute path
max_retries = 2
retry_backoff_seconds = 30
run_budget_seconds = 600
# Fallback notice address when a trigger has no allowlist (cron / manual)
notice_to = "you@example.com"

[loop.triggers.receipts]
kind = "loop"
task = "expense-report"
instance = "trip-1"                 # until instance resolution (#504) lands
allowlist = ["you@example.com"]     # full addresses only; required for mail triggers
profiles = ["work"]                 # empty = every configured profile
subject_tag = "[trip]"              # optional substring filter
attachment_types = [".pdf", ".png", ".jpg"]

[cron.eod-timesheet]
task = "time-sheet"
instance = "current"
schedule = "0 18 * * 1-5"           # UTC minute grid; see cron docs in help
timezone = "America/Toronto"
working_days_only = true
default_hours = 8.0
```

Mail triggers (`kind = "loop"`) require an `allowlist`. Spoof resistance uses the first `Authentication-Results` header (SPF + DKIM + DMARC pass) before enqueue. Cron triggers live under `[cron.<name>]` (not under `loop.triggers`).

## Operate

```bash
blumkin loop init                          # silent refresh / login for mail profiles
blumkin loop init --profile work           # also clears a paused mail profile
blumkin loop list
blumkin loop status --json
blumkin loop run-once receipts             # one foreground run (honors pause / kill switch)
blumkin loop pause receipts
blumkin loop resume receipts
blumkin loop install-service               # systemd --user unit
systemctl --user enable --now blumkin-loop
blumkin loop run                           # foreground equivalent of the service
```

State lives under `~/.config/blumkin/loop/` (queue, journal, locks, handled message ids, mail-profile pause file, optional `KILL`). Do not commit that directory.

## Notices

After a journaled run (`done`, `needs_user`, or `failed`), the runner sends a short plain-text mail to the trigger's first allowlisted address, or to `[loop] notice_to` when there is no allowlist. Bodies carry ids and outcomes only; currency-like tokens in the reason are replaced with `[amount]`. Profile pause notices (auth renewal failure) go to the same recipient and are drafted from the **default** profile, not the paused mailbox. Subjects start with `blumkin loop:` and the mail poller ignores those subjects so notices cannot re-enqueue themselves.

## Security notes

- **Allowlist is the trust boundary.** Only full email addresses; partial domains are refused at config parse time.
- **Message-as-data.** Subject, body and attachment text are not instructions. Do not paste them into a prompt as commands.
- **Kill switch.** Create `~/.config/blumkin/loop/KILL` or set `loop.kill_switch = true` to skip every run without stopping the service.
- **Per-profile pause.** If silent credential renewal fails for one mailbox, that profile pauses; others keep running. Fix with `blumkin loop init --profile <name>`.
- **No unattended explore or submit.** Loop handlers call the existing browser tasks; explore and submit remain out of scope.
- **Notices are best-effort.** A failed send is logged and does not fail the run; Graph calls for notices time out after 30 seconds and run outside the instance lock.

## Outcomes

| Outcome | Meaning |
|---------|---------|
| `done` | Handler finished; journaled |
| `failed` | Retries exhausted or kill switch / budget mid-run |
| `needs_user` | Missing handler, unsigned-in browser, site drift, or other operator action |

`blumkin loop status --json` shows the kill switch path, paused triggers, paused mail profiles, and recent journal rows.
