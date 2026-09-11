# Agent integration

How coding agents reach blumkin, and what they can rely on.

The integration is deliberately thin: blumkin is a CLI with a stable `--json`
contract, and agents run it through the shell tool they already have. There is
also an optional stdio MCP adapter (`blumkin mcp serve`, see [MCP
server](#mcp-server) below) that exposes each skill as a typed tool over the same
execution path — see [`PLAN.md` §6.1](../PLAN.md) for the design.

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
Agents invoke `blumkin`, never `uv run blumkin`. (The exceptions are the
operator-config read skills — `people context`, `tasks list`, `tasks show` — which
read local markdown files and need no `auth login`, no network, and no provider.)

```bash
pipx install blumkin && pipx ensurepath   # no clone needed
blumkin --version                          # version, commit, resolved path
blumkin auth login                         # once per machine; needs a browser
```

`uv tool install -e .` from a clone is the path for working on blumkin itself.
Either way, `blumkin --version` prints the commit and the binary that answered —
paste that line when a session's behavior does not match these docs, so it is
clear whether the install is current.

Auth lives on the machine that runs the agent (`~/.config/blumkin/`). A headless
host needs its own prior `blumkin auth login`; tokens are not portable between
machines.

---

## Multi-profile protocol

Operators may keep **multiple accounts** in one `config.toml` (for example
Microsoft work and Google personal). Agents must not guess which mailbox to use.

1. Cold-start: run `blumkin profiles list --json`.
2. If `count` is `0`, tell the user to configure
   `$BLUMKIN_CONFIG_DIR/config.toml` when `BLUMKIN_CONFIG_DIR` is set;
   otherwise use `~/.config/blumkin/config.toml`.
3. If `count` is `1`, that profile is fine without `--profile`.
4. If `count` is greater than `1`, require an explicit choice before any
   calendar/mail/chat command:
   - User or session tag (`@work`, `@personal`, `google`, …) matching a profile
     `tags` entry, or
   - `--profile <name-or-tag>` on the CLI (wins over `BLUMKIN_PROFILE`), or
   - Ask the user which account; cache the answer for the session only.
5. Fail closed on ambiguity — do not fall back to a silent “default” when more
   than one profile exists and neither `--profile` / `BLUMKIN_PROFILE` nor a
   valid `default_profile` resolves uniquely. A selector that matches one
   profile's name and a *different* profile's tag is ambiguous (`@tag` is
   tag-only).

Safe discovery keys only (`name`, `provider`, `email`, `tags`, `default_tz`,
`auth_present`, `is_default`, plus envelope `count` / `default_profile`). Never
read `config.toml` or token files for secrets.

Flat top-level keys (no `[profiles.*]`) are not a valid `config.toml` layout -
`profiles list` and any command needing config fail closed with a usage error
naming the offending keys. Separate directories via `BLUMKIN_CONFIG_DIR` still
work but are not the preferred multi-account setup; use named profiles instead.

Optional **operator-context files** (`email-context.md`, `tasks/<name>.md`)
follow the same per-profile shape: a `<config-dir>` base, merged with the active
profile's `profiles/<name>/…`. `people.context` / `tasks list` surface the merged
result and flag cross-file conflicts. See
[`operator-config.md`](./operator-config.md).

For a recurring job, an agent should: `tasks list --json` → match the request to
a template's `trigger` → **confirm the pick with the user** → `tasks show --name
<it> --json` → run the returned `prompt` (fetching any input via `mail` / `drive`
itself). blumkin selects nothing and runs no model.

---

## Cursor

### Project skill (shipped)

[`.cursor/skills/blumkin/SKILL.md`](../.cursor/skills/blumkin/SKILL.md) is checked
into this repo, so sessions **in this repo** pick it up with no setup.

### Personal skill (any repo)

To use blumkin from sessions in *other* repos, install it as a personal skill.
This needs a clone of the source — a `pipx install blumkin` ships the CLI but
not the `.cursor/skills/blumkin/` directory, so clone the repo somewhere stable
(it does not have to be the install you run) and point the skill at it.

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
- Exit 1 (`transient_error`): a network/server hiccup talking to the auth
  provider, not a bad grant. Safe to retry the same command once.
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
  "build": {
    "commit": "1f4e9ab2c7d0",
    "version": "0.1.0"
  },
  "cli": "blumkin",
  "ok": true,
  "skills": [
    {
      "args": [
        {
          "name": "--date",
          "required": false,
          "type": "date",
          "param": "day",
          "coerce": "date"
        },
        {
          "name": "--calendar",
          "required": false,
          "type": "string",
          "param": "calendar"
        },
        {
          "name": "--tz",
          "required": false,
          "type": "iana_tz",
          "param": "tz_name"
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

`test_documented_sample_matches_real_output` parses this block and pins `cli`,
`version`, and every shown skill against the live catalog, so those cannot
quietly drift from what the CLI prints. `build` is illustrative only — its
`version` / `commit` track whatever blumkin you have installed (the test checks
its shape, not the values here). Do not compare the values above against your
own `blumkin --version`; run `blumkin --version` itself for that.

| Field | Meaning |
|-------|---------|
| `build` | The blumkin that produced this catalog: `version` (package version) and `commit` (short git SHA, or `unknown`). **Not** the schema version |
| `cli` | Binary name to invoke |
| `ok` | `true` — see [Success envelope](#success-envelope) |
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

Each arg has `name`, `required`, `type`, and `param`; optionally `coerce`,
`values` (for `enum`), `multiple`, and `note`. Types are `date`, `datetime`,
`duration`, `email`, `enum`, `flag`, `iana_tz`, `int`, `path`, `string`.

`param` is the internal name blumkin binds the value to (so `--from` on
`mail.list` is `param: "sender"`, `--tz` is `param: "tz_name"`); `null` means the
value is never passed to a worker method as a direct argument — it is consumed by
a gate, folded into another argument (`calendar view`'s `--from` / `--to` become
a single `[start, end)` range), or the command is bespoke CLI-only plumbing
(`auth *`, `doctor`, `skills *`, `mail signature`, `mcp serve`). Agents driving
the CLI pass `name`; the MCP server (`blumkin mcp serve`) uses `param`-derived
tool schemas. The catalog is pinned against the live code so `param` cannot drift.

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
| 1 | `graph_error`, `install_failed`, `secret_write_failed`, `timeout`, `transient_error`, `upgrade_failed` | Unexpected Graph failure; `completion --install` could not write the script (directory at the path, unwritable dir); local secret cache/auth-record write failed (e.g. symlink at the path); Graph/token HTTP timed out; a transient network/server error talking to the auth provider (safe to retry, not a bad grant); or a `blumkin upgrade` step (pipx / uv tool / git pull / reinstall) could not run or exited non-zero |
| 2 | `usage_error`, or none | Bad arguments; **`wo1162425_scopes` switched off**; or **`people resolve` ambiguous** (`ok: false` + `ambiguous: true` + candidates on **stdout**, no stderr envelope) |
| 3 | `auth_required` | Run `blumkin auth login` on this machine |
| 4 | `missing_scope` | A scope is unavailable — the tenant has not granted it, or `files_scopes` is off |
| 5 | `not_found` | The named thing does not exist |

The config opt-ins do **not** share an exit code, so do not treat "opt-in is
off" as a single condition:

- `wo1162425_scopes` off — chat write, meeting commands, and `people resolve`
  exit **2** with `usage_error`. (`calendar create` Teams meetings use
  Calendars.ReadWrite only and do not require this flag.)
- `docs_scopes` off (Microsoft only) — `docs create`, `docs update`, **and
  every `drive *` verb** exit **2** with `usage_error`; they need
  `Files.ReadWrite` (there is no separate drive toggle). On a Google profile these need no opt-in, but the
  first call after upgrading prompts a one-time re-consent for the `drive`
  scope (`blumkin auth login`).
- `drive read` is **Google only** — on `provider = "microsoft"` it exits **2**
  (Graph has no Word content API; use `drive export --to out.pdf`). `drive
  export` on Microsoft accepts `--to *.pdf` only; any other extension is exit 2.
  `drive download` refuses any Google-native type (folder, shortcut, Doc,
  Sheet, …) with exit 2; `drive export` refuses a format the item's
  `exportLinks` do not offer (also exit 2) rather than failing mid-download.
- `people resolve` ambiguous — also exit **2**, but **stdout** carries
  `ok: false`, `ambiguous: true`, and the candidate list (no stderr
  `usage_error` envelope). Branch on `ok` / `ambiguous` before treating exit 2
  as bad arguments or a missing config flag.
- `files_scopes` off — `chat attachments download` exits **4** with
  `missing_scope` and the share URL in the message. Listing still works.

The practical consequence is that neither code means one thing on its own. Exit
2 is usually a malformed command but sometimes a config change the operator must
make; exit 4 is usually a tenant grant you cannot fix locally but sometimes that
one local flag. In both cases the `message` says which, so read it before
telling the user what to do.

### Success envelope

Every `--json` payload printed on **stdout** is a JSON object with a top-level
`ok` boolean:

- `ok: true` on success — the rest of the object is the command's result.
- `ok: false` on a fail-closed result that still prints to stdout rather than
  the stderr error envelope: `blumkin doctor` (exit 3), `blumkin chat last` with
  no matching chat (exit 5), `blumkin people resolve` when the name is ambiguous
  (exit 2). The payload itself is the diagnosis in these cases.

So an agent can branch on `ok` without inspecting the exit code, then fall back
to the exit code and `error` value for classification. Fields other than `ok`
are command-specific and not frozen unless listed above.

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
| `blumkin chat last --json` | 5 | `ok: false`, `chat: null`, plus `query`, `partial`, and `skipped`, showing how far the search got |
| `blumkin people resolve --json` | 2 | `ok: false`, `ambiguous: true`, and the candidate list |

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
| `build` | Present at the top level; `version` / `commit` strings that track the installed blumkin, not the schema. Informational — never gate on it |
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

## MCP server

`blumkin mcp serve` runs a stdio [Model Context
Protocol](https://modelcontextprotocol.io) server: every skill with a worker
method becomes a typed tool named by its id (`calendar.today`,
`mail.send-draft`, …). The CLI-only verbs — `auth *`, `doctor`, `skills *`,
`mail signature`, and `mcp serve` itself — are not exposed. Tools are generated
from `skills list --json` and dispatched through the same `run_skill` path the
CLI uses, so the CLI stays the single source of truth.

Running the server needs the optional extra:

```bash
pipx install 'blumkin[mcp]'        # or: uv tool install 'blumkin[mcp]'
```

Auth is unchanged — run `blumkin auth login` on a TTY once; every registered
server shares the same `~/.config/blumkin/` token cache. The host spawns
`blumkin mcp serve` as a child process and reaps it when the session ends; there
is no daemon.

### Register it: `blumkin mcp install`

```bash
blumkin mcp install
```

Detects Claude Code, Cursor, and GitHub Copilot CLI, asks whether to write **user**
scope (every repo) or **project** scope (this directory), offers the serve
options, then confirms each client before touching it:

```
Scope (user = every repo, project = this directory) [user]:
Restrict to read-only (non-mutating) tools? [y/N]:
Add blumkin for Claude Code -> claude mcp add (scope: user) [Y/n]:
Add blumkin for Cursor -> ~/.cursor/mcp.json [Y/n]:
Add blumkin for GitHub Copilot CLI -> copilot mcp add (scope: user) [Y/n]:
```

It uses each client's own `mcp add` where it has one — `claude mcp add` at either
scope, `copilot mcp add` at user scope — and merges the entry into the JSON
config otherwise (Cursor at either scope; Copilot at project scope → `.mcp.json`),
leaving any other servers alone. **Re-running is safe** — an entry
that already matches is reported "already current", a stale one (say, after the
`blumkin` binary moved) is updated. Non-interactive: `blumkin mcp install --yes
--scope user` (or `--client cursor`, `--read-only`, `--profile work`,
`--only calendar --only mail`, `--force`).

`blumkin mcp status` shows where blumkin is registered and with which command.

### Register it by hand

If you'd rather not run the installer:

```bash
# Claude Code
claude mcp add --transport stdio blumkin -- blumkin mcp serve
```

```jsonc
// Cursor — .cursor/mcp.json (project) or ~/.cursor/mcp.json (global)
{ "mcpServers": { "blumkin": { "command": "blumkin", "args": ["mcp", "serve"] } } }
```

```jsonc
// GitHub Copilot CLI — ~/.copilot/mcp-config.json (or `copilot mcp add`)
{ "mcpServers": { "blumkin": { "type": "stdio", "command": "blumkin", "args": ["mcp", "serve"], "tools": ["*"] } } }
```

Serve options: `--profile <name>` acts as that profile; `--read-only` exposes only
non-mutating tools; `--only <prefix>` (repeatable) keeps only tools under an id
prefix, e.g. `blumkin mcp serve --only calendar --only mail`.

### Choosing the account per call

Without `--profile`, the server picks the account **per tool call**:

- **Two or more profiles** in `config.toml` → every provider-backed tool gains a
  **required `profile`** string argument (`enum` of the configured names). A call
  without it fails closed with a `usage_error` tool-call error result rather than
  guessing. The server also exposes a read-only **`profiles.list`** tool (name,
  provider, email, tags, default) and sets `InitializeResult.instructions` telling the
  agent to **ask the user** which account when the request is ambiguous (e.g.
  "email my sister" with both a work and a personal profile) — never fall back to
  the default.
- **Exactly one profile** → `profile` is omitted from the schema; the single
  account is used.
- **`--profile <name>`** pins the server to one account, drops `profile` from
  every schema, and rejects an inbound `profile` argument.

A provider/verb combination with no backend for the chosen account (e.g.
`drive.read` on a Microsoft profile) fails at call time with a `usage_error`
naming the reason, not a generic 404/500.

`--read-only` / `--only` stay **server-scoped**. For "personal is read-only, work
is read-write", run a second pinned server:
`blumkin mcp serve --profile personal --read-only` alongside the shared one.

Every tool whose CLI form requires `--yes` carries a **required `confirm: true`**
argument the server enforces — the MCP mirror of the `--yes` gate. That is the
notifying skills (calendar RSVP/create/cancel, `chat.send`, `mail.send-draft`),
the `mail.delete` / `mail.mark` / `mail.move` safety confirms, the
`drive.mkdir` / `drive.move` / `drive.rename` writes, and the
`mail.auto-reply` / `meeting.transcription` setting changes. Those tools also
carry `anthropic/requiresUserInteraction` metadata, and every tool sets
`readOnlyHint` / `destructiveHint`, so MCP clients can prompt appropriately.
(`mail.forward` only drafts a forward, so it needs no confirm.) The three
`drive` write verbs are also hidden under `--read-only`; the read verbs
(`drive.list` / `.get` / `.download` / `.export` / `.read`) stay visible.

v1 is stdio only. A loopback HTTP transport is a later option if a host needs it.

---

## Anti-patterns

- Writing ad-hoc Graph or Azure SDK code when a blumkin skill exists
- Hardcoding a command list instead of reading `skills list --json`
- Adding `--yes` to make a failing command succeed
- Putting client ids or secrets in a skill or instructions file
- Running a `notifies_others` skill to test something
- Inventing SMTP addresses from display names, or probing `calendar freebusy`
  as a people directory — call `blumkin people context` (the operator's
  `email-context.md`) and/or `blumkin people resolve` (Graph directory),
  **confirm the address with the user**, then pass the real email. blumkin does
  not turn a name into an address for you — `--to` / `--cc` / `--with` are
  email-only. Ask the user when `ambiguous: true` or `conflict: true`.
- Inventing colored HTML mail signatures per draft (use `[mail.signature]` /
  `--no-signature` instead). If `mail signature --json` shows `suppressed: true`,
  the account's Outlook adds its own signature and blumkin is deliberately not
  appending `[mail.signature]` — do not force it back in.
- Hand-writing HTML for a mail body — `mail draft` / `reply` / `forward` take
  Markdown by default and render it to HTML; pass `--body-type text` only when a
  literal plain-text body is wanted
- Re-login looping on exit `1` / `secret_write_failed` instead of fixing the
  config path
- Opening a second Graph client when `chat attachments download` returns
  `missing_scope` — give the user the share URL instead
