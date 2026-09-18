---
description: New CLI commands must ship with a worked epilog example
globs: src/blumkin/cli.py,src/blumkin/help_text.py
alwaysApply: false
---

# CLI epilog convention

Every `@main.command` / `@main.group` / leaf `@<group>.command` in
`src/blumkin/cli.py` must pass an `epilog=` sourced from a constant in
`src/blumkin/help_text.py`, following the existing style (see
`MAIL_INBOX_EPILOG`, `MAIL_LIST_EPILOG`):

- A `\b`-prefixed `Example:` / `Examples:` block with 1-4 realistic
  invocations. Prefer `--json` for read/query commands; mutation commands
  (e.g. `mail move` / `mail mark` / `mail delete`) may omit `--json` but
  should show the required confirm flag (e.g. `--yes`).
- 1-3 lines of prose clarifying non-obvious constraints (scopes needed,
  provider differences, batching/skip semantics) — the way
  `MAIL_INBOX_EPILOG` documents `--search`'s scope.

When adding a new command, add its epilog constant to `help_text.py` in the
same PR — do not ship a command with a bare `--help` and no epilog, and do
not leave a new leaf command permanently sharing a sibling's epilog unless
the commands are truly interchangeable (e.g. `calendar decline` /
`calendar tentative` sharing `CALENDAR_DECLINE_EPILOG`, which documents
both explicitly).

See issue #312 for the audit that established this baseline (every command
already had an epilog with a worked example as of that audit).
