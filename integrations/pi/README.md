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

Pi's `git:` source installs the whole repository and expects the package
manifest at the repo root, so it cannot target this subdirectory directly.
Until `@the-hcma/blumkin-pi-skill` is published to npm, install from a local
clone instead — pi's local-path source loads a directory in place, without
copying, so the symlink above still resolves correctly:

```bash
git clone https://github.com/the-hcma/blumkin
pi install ./blumkin/integrations/pi
```

Or, once published to npm:

```bash
pi install npm:@the-hcma/blumkin-pi-skill
```

See [`docs/agent-integration.md`](../../docs/agent-integration.md#pi) for the
full walkthrough, including the zero-install path (pi auto-discovers
`.agents/skills/` when a session is opened inside this repo).
