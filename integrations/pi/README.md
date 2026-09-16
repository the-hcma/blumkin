# blumkin — Pi package

This directory packages blumkin's [Agent Skill](https://agentskills.io) for
[pi.dev](https://pi.dev) (`@earendil-works/pi-coding-agent`), the terminal
coding-agent harness.

It contains **no Python code** — only a `package.json` manifest tagged with
the `pi-package` keyword and a symlink to the canonical skill content at
[`../../.agents/skills`](../../.agents/skills). The `blumkin` CLI itself must
still be installed separately and present on `PATH` (see the repo root
[`README.md`](../../README.md)); this package only teaches pi *when and how*
to shell out to it.

## Install

```bash
# Directly from this repo (no npm publish required); the fragment after `#`
# points pi at this subdirectory of the monorepo:
pi install git:github.com/the-hcma/blumkin@v1.2.1#integrations/pi

# Or, once published to npm:
pi install npm:@the-hcma/blumkin-pi-skill
```

See [`docs/agent-integration.md`](../../docs/agent-integration.md#pi) for the
full walkthrough, including the zero-install path (pi auto-discovers
`.agents/skills/` when a session is opened inside this repo).
