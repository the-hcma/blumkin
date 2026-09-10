# Operator context (`~/.config/blumkin/*.md`)

Optional plain-markdown files that let blumkin (and an agent driving it) act with
more of *your* context. They live next to `config.toml` and the token cache, are
**never written or committed by blumkin**, and are absent by default — nothing
here changes behaviour until you add a file.

| File | Purpose | Skill |
|------|---------|-------|
| `email-context.md` | name / alias → address + relationship notes | `blumkin people context` |

> `task-templates.md` (named reusable prompts) is planned — see
> [#209](https://github.com/the-hcma/blumkin/issues/209).

## Location and precedence

Each file is looked up in two places and **merged**, config-dir first as the
base, then the active profile's copy on top:

1. `$BLUMKIN_CONFIG_DIR/<file>` (else `~/.config/blumkin/<file>`)
2. `<config-dir>/profiles/<active-profile>/<file>`

A legacy flat config (no `[profiles.*]`) makes those the same path. When the same
key appears in both files with **different** values, the merged entry is flagged
(`conflict: true`, both source paths) — blumkin never silently picks one; you
reconcile the files.

Keep these under version control in your own **private** dotfiles repo if you
want history — do not commit them here (they are in `.gitignore` with the rest of
`~/.config/blumkin/`).

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
