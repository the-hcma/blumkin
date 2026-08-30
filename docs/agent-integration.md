# Agent integration

How coding agents reach blumkin, and what they can rely on.

The integration is deliberately thin: blumkin is a CLI with a stable `--json`
contract, and agents run it through the shell tool they already have. There is no
MCP server in v1 — see [`PLAN.md` §6.1](../PLAN.md) for why, and §6 for the
broader design.

```text
Cursor / Copilot CLI / Claude
     │  (reads a skill or instructions file)
     ▼
shell: blumkin calendar today --json
     ▼
Microsoft Graph (delegated, cached auth)
```

---

## Prerequisite: `blumkin` on `PATH`

Every integration below assumes the binary resolves and the machine is signed in.
Agents invoke `blumkin`, never `uv run blumkin`.

```bash
uv tool install -e .   # from a clone
blumkin --version
blumkin auth login     # once per machine; needs a browser
```

Auth lives on the machine that runs the agent (`~/.config/blumkin/`). A headless
host needs its own prior `blumkin auth login`; tokens are not portable between
machines.

---

## Multi-profile protocol

Operators may keep **multiple accounts** in one `config.toml` (for example
Microsoft work and Google personal). Agents must not guess which mailbox to use.

1. Cold-start: run `blumkin profiles list --json`.
2. If `count` is `0`, tell the user to configure `~/.config/blumkin/config.toml`.
3. If `count` is `1`, that profile is fine without `--profile`.
4. If `count` is greater than `1`, require an explicit choice before any
   calendar/mail/chat command:
   - User or session tag (`@work`, `@personal`, `google`, …) matching a profile
     `tags` entry, or
   - `--profile <name-or-tag>` on the CLI (wins over `BLUMKIN_PROFILE`), or
   - Ask the user which account; cache the answer for the session only.
5. Fail closed on ambiguity — do not fall back to a silent “default” when more
   than one profile exists and neither `--profile` / `BLUMKIN_PROFILE` nor a
   valid `default_profile` resolves uniquely.

Safe discovery keys only (`name`, `provider`, `tags`, `default_tz`,
`auth_present`, `is_default`, plus envelope `count` / `default_profile`). Never
read `config.toml` or token files for secrets.

Legacy single-file layouts (flat keys, no `[profiles.*]`) appear as one profile
named `default`. Separate directories via `BLUMKIN_CONFIG_DIR` still work but are
no longer the preferred multi-account setup.

---

## Cursor

### Project skill (shipped)

[`.cursor/skills/blumkin/SKILL.md`](../.cursor/skills/blumkin/SKILL.md) is checked
into this repo, so sessions **in this repo** pick it up with no setup.

### Personal skill (any repo)

To use blumkin from sessions in *other* repos, install it as a personal skill.

**Symlink** — tracks the clone, so the skill follows `git pull`:

```bash
mkdir -p ~/.cursor/skills
ln -s ~/work/ai/blumkin/.cursor/skills/blumkin ~/.cursor/skills/blumkin
```

**Copy** — pinned, and survives moving or deleting the clone:

```bash
mkdir -p ~/.cursor/skills
cp -R ~/work/ai/blumkin/.cursor/skills/blumkin ~/.cursor/skills/blumkin
```

Prefer the symlink unless you need the skill to outlive the clone; a copy goes
stale silently as commands are added. Verify with `ls ~/.cursor/skills/blumkin/`
and by asking a session in an unrelated repo what's on your calendar.

Both skills come from the same source file, so in a session inside this repo
they normally say the same thing and precedence does not matter. That holds
exactly as long as they agree — a symlink keeps them identical, while a copy can
drift behind the project skill without any visible sign. If a session in this
repo behaves differently from one outside it, suspect a stale copy first and
refresh it by re-running the `cp -R` above.

---

## GitHub Copilot CLI

Copilot CLI has no skills directory; it reads Markdown instruction files. For a
personal tool that should work across every repo, use the **user-level** file:

| Path | Scope |
|------|-------|
| `$HOME/.copilot/copilot-instructions.md` | All repos on this machine |
| `.github/copilot-instructions.md` | One repo |
| `AGENTS.md` | One repo, shared with other agents |

Append this to `~/.copilot/copilot-instructions.md`:

```markdown
## Personal Microsoft 365 (blumkin)

For the user's own Outlook calendar, mail, Teams chat, or free/busy, shell out to
`blumkin` instead of writing Microsoft Graph or Azure SDK code. Never invent a
client id or call Graph directly when a blumkin skill covers the job.

- Discover commands with `blumkin skills list --json`; describe one with
  `blumkin skills describe <id> --json`.
- Always pass `--json` when parsing output.
- Writes that notify other people require `--yes`. Never add `--yes` to satisfy a
  failed command — only when the user asked for that action.
- Exit 3 (`auth_required`): tell the user to run `blumkin auth login` on this
  machine, then retry. Do not attempt to authenticate any other way. Exception:
  `blumkin doctor` exits 3 for a missing `client_id` too — read its `problems`
  array before advising.
- Exit 1 (`secret_write_failed`): the MSAL cache or auth record could not be
  written (often a symlink under the config dir). Fix the path; do not loop on
  `auth login`.
- On any non-zero exit, if stderr is empty the explanation is on stdout
  (`doctor`, `chat last`). Never report "no output".
- Exit 4 (`missing_scope`): a scope is unavailable — usually a tenant grant, but
  sometimes a local opt-in. Report the message; do not retry. For chat file
  downloads, hand the share URL to the user instead of inventing Graph calls.
- Exit 2 (`usage_error`): usually a malformed command, but sometimes a config
  opt-in that is off. Read the message before calling it bad arguments.
```

Keep it short. It competes with everything else in the context window, and the
detail belongs in `blumkin skills list --json`, which is always current.

---

## The contract agents depend on

Frozen as of **schema version 1**. Everything in this section is what an agent may
rely on; anything not listed may change.

### `blumkin skills list --json`

Real output, with `skills` cut to one entry — the full list carries every skill:

```json
{
  "cli": "blumkin",
  "skills": [
    {
      "args": [
        {
          "name": "--date",
          "required": false,
          "type": "date"
        },
        {
          "name": "--tz",
          "required": false,
          "type": "iana_tz"
        }
      ],
      "cli": [
        "blumkin",
        "calendar",
        "today"
      ],
      "id": "calendar.today",
      "mutates": false,
      "notifies_others": false,
      "scopes": [
        "Calendars.ReadWrite"
      ],
      "summary": "List the signed-in user's events for today"
    }
  ],
  "version": 1
}
```

`test_documented_sample_matches_real_output` parses this block out of the file
and compares it against the live catalog, so it cannot quietly drift from what
the CLI actually prints.

| Field | Meaning |
|-------|---------|
| `cli` | Binary name to invoke |
| `version` | Schema version — `1` |
| `skills` | Every skill, **sorted by `id`** |

Each skill:

| Field | Meaning |
|-------|---------|
| `id` | Stable identifier, usually `area.verb` (`mail.attachments.download`, or bare `doctor`). Accepted by `skills describe` |
| `cli` | Argv prefix to run, already split |
| `summary` | One line, human-readable |
| `mutates` | Changes server-side state |
| `notifies_others` | **Reaches another person** — invites, sends, chats |
| `scopes` | Graph scopes required |
| `args` | Accepted arguments, in the order they read naturally on the command line — **not** sorted |

Each arg has `name`, `required`, and `type`; optionally `values` (for `enum`),
`multiple`, and `note`. Types are `date`, `datetime`, `duration`, `email`, `enum`,
`flag`, `iana_tz`, `int`, `path`, `string`.

`name` is normally an option (`--folder`), but may be a positional with no leading
dash — `skills.describe` takes `skill-id`. Build the command from `cli` plus these
args rather than assuming every name is a flag.

`notifies_others` is the field to key safety decisions on — it is the same flag
[`.cursor/rules/no-third-party-side-effects.mdc`](../.cursor/rules/no-third-party-side-effects.mdc)
uses to decide what must never run as a test. Every skill carrying it also
declares a required `--yes`, which the schema tests enforce. Those tests also
require each skill to be classified explicitly, so the flag cannot be left unset
on a new command or quietly dropped from an existing one without someone
deciding that it does not reach anyone.

`blumkin skills describe <id> --json` returns a single skill object with the same
shape, without the envelope.

### Exit codes

Branch on the **exit code first** — it is the only signal present on every
failure path.

| Code | `error` value | Meaning |
|------|---------------|---------|
| 0 | — | Success |
| 1 | `graph_error`, `secret_write_failed`, `timeout` | Unexpected Graph failure; local secret cache/auth-record write failed (e.g. symlink at the path); or Graph/token HTTP timed out |
| 2 | `usage_error`, or none | Bad arguments; **`wo1162425_scopes` switched off**; or **`people resolve` ambiguous** (`ambiguous: true` + candidates on **stdout**, no stderr envelope) |
| 3 | `auth_required` | Run `blumkin auth login` on this machine |
| 4 | `missing_scope` | A scope is unavailable — the tenant has not granted it, or `files_scopes` is off |
| 5 | `not_found` | The named thing does not exist |

The two config opt-ins do **not** share an exit code, so do not treat "opt-in is
off" as a single condition:

- `wo1162425_scopes` off — chat write, meeting commands, and `people resolve`
  exit **2** with `usage_error`. (`calendar create` Teams meetings use
  Calendars.ReadWrite only and do not require this flag.)
- `people resolve` ambiguous — also exit **2**, but success-shaped **stdout**
  carries `ambiguous: true` and the candidate list (no stderr `usage_error`
  envelope). Branch on `ambiguous` / the candidate list before treating exit 2
  as bad arguments or a missing config flag.
- `files_scopes` off — `chat attachments download` exits **4** with
  `missing_scope` and the share URL in the message. Listing still works.

The practical consequence is that neither code means one thing on its own. Exit
2 is usually a malformed command but sometimes a config change the operator must
make; exit 4 is usually a tenant grant you cannot fix locally but sometimes that
one local flag. In both cases the `message` says which, so read it before
telling the user what to do.

### Error envelope

Most failures with `--json` print one object to **stderr**, leaving stdout empty:

```console
$ blumkin skills describe nope --json
{"error": "not_found", "message": "Unknown skill: nope", "ok": false}
```

Capture the two streams separately. An agent that parses only stdout sees an
empty string on these failures and learns nothing about what went wrong.

| Field | Meaning |
|-------|---------|
| `ok` | Always `false` in this envelope |
| `error` | Stable value to branch on — see the exit-code table |
| `message` | For humans; wording will change, so do not match on it |
| `hint` | Optional next step, present only when the CLI has one to offer |

#### Diagnostic commands report failure on stdout instead

Two commands exit non-zero with their normal JSON payload on **stdout** and
nothing on stderr, because the payload *is* the diagnosis:

| Command | Exit | What stdout carries |
|---------|------|---------------------|
| `blumkin doctor --json` | 3 | `ok: false` and a `problems` array naming what is wrong |
| `blumkin chat last --json` | 5 | `chat: null` plus `query`, `partial`, and `skipped`, showing how far the search got |

So the rule is: **on a non-zero exit, if stderr is empty, parse stdout.** Do not
report "no output" — the explanation is there, in the stream you did not read.

`doctor` matters most here. Exit 3 normally means run `blumkin auth login`, but
`doctor` also exits 3 when `client_id` is missing from `config.toml`, which no
amount of logging in will fix. Read `problems` before advising the user.

Three caveats worth wiring in up front:

- **`error` values are not the exit-code names.** They are `graph_error`,
  `usage_error`, and `secret_write_failed` (among others), not `other` and
  `usage`. Match the `error` values in the table's `error` column. Use the exit
  code first for routing, then use `error` for stable failure classification.
- **Argument errors may arrive with no envelope.** Bad or missing options are
  rejected by the argument parser before blumkin runs, so exit 2 can carry plain
  usage text on stderr instead of JSON. Treat a missing envelope on exit 2 as a
  malformed command, not as a transient failure to retry.
- **An empty stderr does not mean success.** Check the exit code first, then
  fall back to stdout, as above.

### Compatibility

Within schema version 1, these hold for a skill that already exists:

| Value | Promise |
|-------|---------|
| `id` | Never renamed or removed |
| `cli` | Always `blumkin` followed by the `id` split on dots — so the invocation is derivable, and a renamed subcommand is a breaking change |
| `mutates`, `notifies_others` | Never change. These are consent metadata; a cached `false` must stay true |
| `args` — names and types | Existing ones never change. New **optional** arguments may appear |
| `args` — `values` on an enum | Existing values are never removed or renamed. New ones may be added |
| exit codes and `error` values | Meanings never change |
| `summary` | **May be reworded** — never match on it |
| `scopes` | **May change** when Graph requirements do — re-read rather than caching |

Whole skills may be added. Fields may be added to any object. A change that
breaks the promises above bumps `version`.

Two consequences worth designing for:

- **Ignore unknown fields** rather than failing on them, so an addition does not
  break you.
- **Match arguments by name, not position.** `args` is ordered for reading, and
  an added optional argument can shift positions.

Every promise in that table is pinned by `tests/test_skills_schema.py` — the two
envelopes and the stream each goes to, the exit codes and error values, the
invocation rule, released ids, consent metadata, and enum values — so a drift is
a test failure rather than a surprise in someone's agent session. The tests
assert the documented fields are **present**, not that no others are, which is
what "fields may be added" has to mean if it is to be true.

Argument names and types are pinned in full for the skills quoted in this guide,
and checked for shape everywhere else, which is why `args` promises stability per
argument rather than a frozen list.

---

## Anti-patterns

- Writing ad-hoc Graph or Azure SDK code when a blumkin skill exists
- Hardcoding a command list instead of reading `skills list --json`
- Adding `--yes` to make a failing command succeed
- Putting client ids or secrets in a skill or instructions file
- Running a `notifies_others` skill to test something
- Inventing SMTP addresses from display names, or probing `calendar freebusy`
  as a people directory — use `blumkin people resolve` and ask the user when
  `ambiguous: true`
- Inventing colored HTML mail signatures per draft (use `[mail.signature]` /
  `--no-signature` instead)
- Re-login looping on exit `1` / `secret_write_failed` instead of fixing the
  config path
- Opening a second Graph client when `chat attachments download` returns
  `missing_scope` — give the user the share URL instead
