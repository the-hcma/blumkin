# blumkin

Personal Microsoft 365 / Graph **skills CLI** — named after Rose “Mrs. B” Blumkin, Berkshire’s legendary operator.

Blumkin turns Graph flows into **small, invokable skills** any coding agent (**Cursor**, **GitHub Copilot**, **Claude**, …) can run via shell — instead of re-implementing auth and Microsoft Graph calls.

It uses **delegated** Microsoft Graph access (acts as the signed-in user).

## Status

Planning + org-practices scaffold. **No application code yet.**

- [`PLAN.md`](./PLAN.md) — **CLI design for review** (commands, JSON, **Cursor/Copilot integration**, lint)  
- [`HANDOFF.md`](./HANDOFF.md) — **session handoff** (proven flows, open Identity follow-up, what to do next)  
- [`AGENTS.md`](./AGENTS.md) — contributor / agent ground rules  

Hand-proven Graph flows are encoded as Blumkin skills once they settle; prefer validating a new flow outside this repo first, then porting it here.

## Intended usage (future)

Install so `blumkin` is on your `PATH` (planned: `uv tool install` / equivalent). Agents and humans invoke the binary directly — not via `uv run`:

```bash
blumkin auth status
blumkin calendar today --json
blumkin skills list --json
```

## Repository practices

This tree is set up to work with
[repository-helpers](https://github.com/the-hcma/repository-helpers) /
`github-repo-lint` (stacking `gh-stack`, Cursor rules, CODEOWNERS, MIT license,
`AGENTS.md`):

```bash
# from a repository-helpers clone:
scripts/github-repo-lint --repo the-hcma/blumkin --suggest
```

## License

MIT — see [`LICENSE`](./LICENSE).
