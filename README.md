# blumkin

Personal Microsoft 365 / Graph **skills CLI** — named after Rose “Mrs. B” Blumkin, Berkshire’s legendary operator.

Blumkin turns Graph flows into **small, invokable skills** any coding agent (**Cursor**, **GitHub Copilot**, **Claude**, …) can run via shell — instead of re-implementing auth and Microsoft Graph calls.

It uses **delegated** Microsoft Graph access (acts as the signed-in user).

## Status

Planning + org-practices scaffold. **No application code yet.**

- [`PLAN.md`](./PLAN.md) — **CLI design for review** (commands, JSON, **Cursor/Copilot integration**, lint)  
- [`HANDOFF.md`](./HANDOFF.md) — **session handoff** (what works in the lab, open Identity follow-up, what to do next)  
- [`AGENTS.md`](./AGENTS.md) — contributor / agent ground rules  

Proven ad-hoc flows live in `~/work/brk-tech/personal-automation` until ported here. **Prefer finishing new hand experiments there first**, then encode them as Blumkin skills.

## Intended usage (future)

```bash
uv run blumkin auth status
uv run blumkin calendar today --json
uv run blumkin skills list --json
```

## Repository practices

This tree is set up to work with
[repository-helpers](https://github.com/the-hcma/repository-helpers) /
`github-repo-lint` (stacking `gh-stack`, Cursor rules, CODEOWNERS, MIT license,
`AGENTS.md`). After a GitHub remote exists:

```bash
~/work/ai/repository-helpers/scripts/github-repo-lint --new-repo --suggest
```

## License

MIT — see [`LICENSE`](./LICENSE).
