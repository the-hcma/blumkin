# blumkin

Personal Microsoft 365 / Graph **skills CLI** — named after Rose “Mrs. B” Blumkin, Berkshire’s legendary operator.

Blumkin turns Graph flows into **small, invokable skills** any coding agent (**Cursor**, **GitHub Copilot**, **Claude**, …) can run via shell — instead of re-implementing auth and Microsoft Graph calls.

It uses **delegated** Microsoft Graph access (acts as the signed-in user).

## Status

**M1 shipped** ([#10](https://github.com/the-hcma/blumkin/pull/10)): packaging, auth under `~/.config/blumkin/`, `skills` / `doctor`, `calendar today`, Cursor skill, hermetic CI + local live tests.

Tracking: [#9 Cursor agent integration (M1 MVP)](https://github.com/the-hcma/blumkin/issues/9).

## Install (`blumkin` on `PATH`)

From a clone (dev):

```bash
uv sync --group dev
uv tool install -e .
```

Then invoke the binary directly — **not** `uv run blumkin`:

```bash
blumkin --version
blumkin auth login          # once per machine / when cache is cold
blumkin auth status
blumkin skills list --json
blumkin calendar today --json
```

`uv tool install` puts `blumkin` on your tool bin dir (often `~/.local/bin`). Ensure that directory is on `PATH`.

To use blumkin from agent sessions in **other** repos (Cursor personal skill, or Copilot CLI instructions), see [`docs/agent-integration.md`](./docs/agent-integration.md).

## Config (`~/.config/blumkin/`)

Create `~/.config/blumkin/config.toml` (mode `0600`):

```toml
client_id = "<entra-public-client-id>"
tenant_id = "<your-entra-tenant>"
default_tz = "<IANA timezone, e.g. America/New_York>"
provider = "microsoft"
```

Set `tenant_id`, `default_tz`, and `provider` in this file (there are no org-specific code defaults). `provider` defaults to `microsoft` when omitted.

Interactive browser auth is public-client only (`client_id` + `tenant_id`). Do not set a client secret for this flow.

Token cache files (written by `blumkin auth login`):

- `~/.config/blumkin/msal_token_cache.json`
- `~/.config/blumkin/auth_record.json`

Override config directory with `BLUMKIN_CONFIG_DIR`. Never commit these files.

## Tests

```bash
uv run pytest -m 'not live'          # CI-equivalent (mocks / offline)
BLUMKIN_LIVE=1 uv run pytest -m live # live Graph reads + silent refresh
```

Live tests need `~/.config/blumkin/` by default (override with `BLUMKIN_CONFIG_DIR`):
`config.toml`, token cache, auth record, and a usable refresh token. Never commit those files.

## Docs

- [`PLAN.md`](./PLAN.md) — CLI design  
- [`HANDOFF.md`](./HANDOFF.md) — session handoff  
- [`AGENTS.md`](./AGENTS.md) — contributor / agent ground rules  
- [`RETROSPECTIVE-M1.md`](./RETROSPECTIVE-M1.md) — M1 ship retrospective (#11)  
- [`docs/agent-integration.md`](./docs/agent-integration.md) — using blumkin from Cursor / Copilot CLI, and the frozen `skills list --json` contract  
- [`.cursor/skills/blumkin/SKILL.md`](./.cursor/skills/blumkin/SKILL.md) — Cursor agent skill  

## License

MIT — see [`LICENSE`](./LICENSE).
