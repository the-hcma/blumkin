---
description: Format agent-authored GitHub issue/PR bodies and comments so they render correctly (blank lines + no hand-wrapped paragraphs)
alwaysApply: true
---

# GitHub content formatting (agent-authored)

Agent-authored issue bodies, issue comments or replies, PR descriptions, and
PR/review comments must render correctly on GitHub. Write multi-paragraph or
list content to a temp file and post with `--body-file` (never inline
`--body "..."` with embedded `\n` escapes); separate paragraphs and list items
with a blank line; and never hard-wrap prose across short physical lines —
GitHub's **issue/PR/comment** renderer treats a lone `\n` as a visible hard
break (unlike the file/blob renderer's soft-break-as-space), so a hand-wrapped
paragraph shows up as a column of disconnected short lines.

Before posting, lint:

```bash
rh="${REPOSITORY_HELPERS_DIR:-$HOME/work/ai/repository-helpers}"
"${rh}/scripts/lint-github-markdown" <path>
```

For issues, use `"${rh}/scripts/gh-issue" create|edit …` so the lint runs
before the API call. For PR bodies / review replies, lint then
`"${rh}/scripts/gh-api" pr edit` or `reply-thread` / `reply-comment`.

Full rationale, worked examples, and the linter's exact checks (the SSOT) —
the canonical rule in repository-helpers:

<!-- github-content-formatting-canonical: https://github.com/the-hcma/repository-helpers/blob/main/.agents/rules/github-content-formatting.md -->
https://github.com/the-hcma/repository-helpers/blob/main/.agents/rules/github-content-formatting.md
