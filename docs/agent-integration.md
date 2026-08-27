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

A personal skill and this repo's project skill are the same file. When both are
present in a session, they say the same thing, so precedence does not matter.

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
  machine, then retry. Do not attempt to authenticate any other way.
- Exit 4 (`missing_scope`): the tenant has not granted a scope. Report the
  message; do not retry.
- Exit 2 (`usage_error`): usually a malformed command, but it is also how a
  disabled config opt-in is reported. Read the message before calling it bad
  arguments.
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
| 1 | `graph_error` | Unexpected failure, usually from Graph |
| 2 | `usage_error`, or none | Bad arguments, **or a config opt-in that is switched off** |
| 3 | `auth_required` | Run `blumkin auth login` on this machine |
| 4 | `missing_scope` | The tenant has not granted a scope — do not retry |
| 5 | `not_found` | The named thing does not exist |

Exit 2 covers two different situations, and conflating them misleads the user.
A malformed command needs fixing; a disabled opt-in (`wo1162425_scopes`,
`files_scopes`) is a config change the operator must make, and the `message`
says which. Read the message before reporting "bad arguments".

Exit 4 is narrower than it sounds: it means Graph refused a scope the tenant has
not granted, so no local change will help.

### Error envelope

Failures with `--json` print one object to **stderr**, leaving stdout empty:

```console
$ blumkin skills describe nope --json
{"error": "not_found", "message": "Unknown skill: nope", "ok": false}
```

Capture the two streams separately. An agent that parses only stdout sees an
empty string on every failure and learns nothing about what went wrong.

| Field | Meaning |
|-------|---------|
| `ok` | Always `false` in this envelope |
| `error` | Stable value to branch on — see the exit-code table |
| `message` | For humans; wording will change, so do not match on it |
| `hint` | Optional next step, present only when the CLI has one to offer |

Two caveats worth wiring in up front:

- **`error` values are not the exit-code names.** They are `graph_error` and
  `usage_error`, not `other` and `usage`. Match the left column of the table
  above rather than the prose name of the code.
- **Argument errors may arrive with no envelope.** Bad or missing options are
  rejected by the argument parser before blumkin runs, so exit 2 can carry plain
  usage text on stderr instead of JSON. Treat a missing envelope on exit 2 as a
  malformed command, not as a transient failure to retry.

### Compatibility

Within schema version 1: fields may be **added**, and skills may be added or have
their `summary` reworded. Existing field names, their types, `id` values, and
exit-code meanings will not change. A breaking change bumps `version`.

Parse defensively — ignore unknown fields rather than failing on them.

The shape is pinned by `tests/test_skills_schema.py`, covering the success
envelope, the error envelope and the stream it goes to, and the exit codes — so
a drift is a test failure rather than a surprise in someone's agent session. The
tests assert the documented fields are **present**, not that no others are, which
is what "fields may be added" has to mean if it is to be true.

---

## Anti-patterns

- Writing ad-hoc Graph or Azure SDK code when a blumkin skill exists
- Hardcoding a command list instead of reading `skills list --json`
- Adding `--yes` to make a failing command succeed
- Putting client ids or secrets in a skill or instructions file
- Running a `notifies_others` skill to test something
