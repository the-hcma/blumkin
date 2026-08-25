# blumkin

Personal Microsoft 365 / Graph **skills CLI** — named after Rose “Mrs. B” Blumkin, Berkshire’s legendary operator.

Blumkin turns Graph flows into **small, invokable skills** any coding agent (**Cursor**, **GitHub Copilot**, **Claude**, …) can run via shell — instead of re-implementing auth and Microsoft Graph calls.

It uses **delegated** Microsoft Graph access (acts as the signed-in user).

Tracking: [#9 Cursor agent integration (M1 MVP)](https://github.com/the-hcma/blumkin/issues/9).

## Install

```bash
uv sync
uv tool install -e .
# then invoke on PATH:
blumkin auth status
blumkin skills list --json
blumkin calendar today --json
```

## Config (`~/.config/blumkin/`)

Create `~/.config/blumkin/config.toml` (mode `0600`):

```toml
client_id = "<entra-public-client-id>"
tenant_id = "brk.tech"
default_tz = "America/New_York"
```

Optional: `client_secret` in the same file or `BLUMKIN_CLIENT_SECRET` (not required for interactive public-client browser auth).

Token cache files (written by `blumkin auth login`):

- `~/.config/blumkin/msal_token_cache.json`
- `~/.config/blumkin/auth_record.json`

Override config directory with `BLUMKIN_CONFIG_DIR`. Never commit these files.

## Tests

```bash
uv run pytest -m 'not live'          # CI-equivalent (mocks / offline)
BLUMKIN_LIVE=1 uv run pytest -m live # live Graph reads + silent refresh
```

Live tests need `~/.config/blumkin/` (config + token cache + auth record). Never commit those files.

## Docs

- [`PLAN.md`](./PLAN.md) — CLI design  
- [`HANDOFF.md`](./HANDOFF.md) — session handoff  
- [`AGENTS.md`](./AGENTS.md) — contributor / agent ground rules  
- [`.cursor/skills/blumkin/SKILL.md`](./.cursor/skills/blumkin/SKILL.md) — Cursor agent skill  

## License

MIT — see [`LICENSE`](./LICENSE).
