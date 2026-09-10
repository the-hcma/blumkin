# Operator context (`~/.config/blumkin/*.md`)

Optional plain-markdown files that let blumkin (and an agent driving it) act with
more of *your* context. They live next to `config.toml` and the token cache, are
**never written or committed by blumkin**, and are absent by default — nothing
here changes behaviour until you add a file.

| File / dir | Purpose | Skill |
|------------|---------|-------|
| `email-context.md` | name / alias → address + relationship notes | `blumkin people context` |
| `tasks/<name>.md` | named reusable prompt templates | `blumkin tasks list` / `tasks show` |

## Location and precedence

Each file is looked up in two places and their entries **combined**:

1. `$BLUMKIN_CONFIG_DIR/<file>` (else `~/.config/blumkin/<file>`)
2. `<config-dir>/profiles/<active-profile>/<file>`

A legacy flat config (no `[profiles.*]`) makes those the same path.

Entries for the same key that **agree** (an address matches; a note fills a blank
or is identical) are merged into one. Entries that **clash** — a different
address, or two different non-empty notes for one name — are **not** merged:
blumkin surfaces each, every one flagged `conflict: true` with its own `sources`,
so you (or an agent) can see both and reconcile the files. blumkin never picks
one and never edits the files.

`email-context.md` is in this repo's `.gitignore`, so a config dir that happens
to sit inside a working tree will not have it committed by accident. Keep it
under version control in your own **private** dotfiles repo if you want history.

## `email-context.md`

A list of people you correspond with. blumkin **does not** turn a name into an
address on its own — `--to`, `--cc`, `--with` stay email-only. This file is a
lookup surface: `blumkin people context` (and the MCP `people.context` tool)
lists it; an agent fuzzy-matches your phrasing against it, **confirms the
recipient with you**, then calls `mail draft` / `calendar create` with the real
address.

Two row forms are read, and may be mixed in one file:

### Table (documented default)

```markdown
| Name | Aliases  | Email           | Notes                          |
|------|----------|-----------------|--------------------------------|
| Sam  | sammy, S | sam@example.com | Colleague on the Foo project.  |
| Alex |          | alex@example.com | Friend; keep it casual.       |
```

`Name` and `Email` are required; `Aliases` (comma- or `;`-separated) and `Notes`
are optional; extra columns are ignored; the header is matched
case-insensitively.

### Bullet

```markdown
- Sam (sammy, S) <sam@example.com> - colleague on the Foo project
- Alex <alex@example.com> - friend; keep it casual
```

`- Name (aliases) <email> - notes`. The `(aliases)` group and the `- notes` tail
are optional.

A row with no valid email is skipped with a warning; the rest of the file still
loads.

### What `notes` is for

`notes` is surfaced verbatim to whoever calls `people context`. It is where tone
and relationship context lives ("my manager", "prefers bullet points", "kids —
keep it playful") so an agent can pick the right register without you
re-explaining each time. It is **not** a secret store — addresses and free text
only.

## `tasks/<name>.md`

One markdown file per template under `tasks/`, so each is easy to diff and
share. The filename stem is the template name (`tasks/weekly-report.md` →
`weekly-report`). `<config-dir>/tasks/` is merged with the active profile's
`profiles/<name>/tasks/`; a template that exists in both with **different**
content is flagged (`conflict: true`) and `tasks show` refuses it until you
reconcile.

```markdown
# Weekly status report

**Trigger:** "weekly report", "status update"
**Input:** the latest thread in the Reports folder
**Output:** five bullets, mailed to the team

**Prompt:**

> Summarise the input as exactly five bullets, most important first.
> Keep each bullet under 20 words.
```

- The leading `# Title` line is optional (defaults to the name).
- `**Trigger:**`, `**Input:**`, `**Output:**` are one-line fields, all optional.
- Everything in the `>` blockquote after `**Prompt:**` is the prompt body.
- A file with no `**Prompt:**` block still lists, with a warning.

### `Trigger:` is a hint, not a matcher

blumkin does **nothing** with `Trigger:` beyond showing it. `tasks list` gives an
agent the triggers; the agent matches the user's request to one, **confirms the
choice with the user**, calls `tasks show --name <it>`, and runs the prompt
itself against whatever input the request implies. blumkin never selects a
template, never resolves the input, and never calls a model.

