---
description: Format agent-authored GitHub issue/PR bodies and comments so they render correctly (blank lines + no hand-wrapped paragraphs)
alwaysApply: true
---

# GitHub content formatting (agent-authored)

Agent-authored issue bodies, issue comments or replies, PR descriptions, and
PR/review comments must render correctly on GitHub.

Two failure modes show up as “line breaks are messed up”:

1. **Missing blank lines / literal `\n` escapes** — assembling bodies as inline
   `--body "line one\nline two"` (or joining with a single `\n` instead of `\n\n`)
   collapses lists and paragraphs in CommonMark-ish ways, and embeds literal
   backslash-n text when the shell does not expand escapes.
2. **Hand-wrapped paragraphs** — wrapping a paragraph across several short
   physical lines with a lone newline between them. GitHub’s **issue/PR/comment**
   renderer treats that lone `\n` as a **visible hard break** (unlike the
   file/blob renderer’s soft-break-as-space). The body looks like a column of
   disconnected short lines. Prefer one long line per paragraph, or separate
   paragraphs with a blank line.

## Authoring

1. **Multi-paragraph or multi-line-list bodies:** write the body to a temp file,
   then post with `--body-file <path>`. Never assemble multi-line content as an
   inline `--body "..."` string with embedded `\n` escapes.
2. **Inline `--body` is only OK** for a genuinely single-line body (no lists,
   no headings, no code fences, no paragraph breaks, no hand-wraps).
3. **Blank lines:** separate paragraphs and list items with a blank line (`\n\n`).
   Put a blank line before and after fenced code blocks. Put a blank line before
   headings (except at the start of the file).
4. **Paragraphs:** do not hard-wrap prose for column width. One physical line per
   paragraph (or blank-line-separated paragraphs).

```bash
rh="${REPOSITORY_HELPERS_DIR:-$HOME/work/ai/repository-helpers}"
body="$(mktemp)"
cat >"$body" <<'EOF'
## Summary

- First item

- Second item

## Test plan

- [ ] Check rendering on GitHub
EOF
"${rh}/scripts/lint-github-markdown" "$body"
# Issues: prefer the linting wrapper
"${rh}/scripts/gh-issue" create --title 'feat: example' --body-file "$body"
# Or PR body:
# "${rh}/scripts/gh-api" pr edit <n> --body-file "$body"
rm -f "$body"
```

## Pre-flight

Before posting (or after drafting a body file), run:

```bash
rh="${REPOSITORY_HELPERS_DIR:-$HOME/work/ai/repository-helpers}"
"${rh}/scripts/lint-github-markdown" <path>
```

For **issues**, use `"${rh}/scripts/gh-issue" create|edit …` so the lint runs before the
API call. For PR bodies / review replies, lint then `"${rh}/scripts/gh-api" pr edit`
or `reply-thread` / `reply-comment`.

The linter flags mid-line list markers, headings lacking a preceding blank line,
fence markers that are not alone on their line, literal `\n` escapes outside
fences, and consecutive prose lines (hand-wrapped paragraphs). Fix findings
before posting.

## Scope

Applies to issue bodies, issue comments or replies, PR descriptions, and
PR/review comments or replies posted by an agent — same “validate before it
ships” principle as Conventional Commits PR titles (see
`.agents/rules/pr-ship-and-review.md`).
