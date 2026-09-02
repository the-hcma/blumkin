# blumkin

Personal Microsoft 365 / Graph **skills CLI** — named after Rose “Mrs. B” Blumkin, Berkshire’s legendary operator.

Blumkin turns Graph flows into **small, invokable skills** any coding agent (**Cursor**, **GitHub Copilot**, **Claude**, …) can run via shell — instead of re-implementing auth and Microsoft Graph calls.

It uses **delegated** Microsoft Graph access (acts as the signed-in user).

## Status

**M1 shipped** ([#10](https://github.com/the-hcma/blumkin/pull/10)): packaging, auth under `~/.config/blumkin/`, `skills` / `doctor`, `calendar today`, Cursor skill, hermetic CI + local live tests.

Tracking: [#9 Cursor agent integration (M1 MVP)](https://github.com/the-hcma/blumkin/issues/9).

## Install (`blumkin` on `PATH`)

```bash
pipx install blumkin
pipx ensurepath          # first pipx install only; opens a new shell
```

Then invoke the binary directly — **not** `uv run blumkin`:

```bash
blumkin --version           # version, commit, and which binary answered
blumkin auth login          # once per machine / when cache is cold
blumkin auth status
blumkin skills list --json
blumkin calendar today --json
```

`pipx` puts `blumkin` in its bin dir (usually `~/.local/bin`); `pipx ensurepath`
makes sure that is on `PATH`.

### Upgrade

```bash
blumkin upgrade
```

Wraps `pipx upgrade blumkin` and prints the version and commit you were on and
the one you moved to — bare `pipx upgrade` cannot tell you whether `PATH` still
resolves to a dev checkout.

### From a clone (developing blumkin)

```bash
uv sync --group dev
uv tool install -e .        # editable; `blumkin` now points at the checkout
```

`blumkin --version` reports the checkout's commit, and `blumkin upgrade` will
say it is running from a source checkout and leave the tree alone.

To use blumkin from agent sessions in **other** repos (Cursor personal skill, or
Copilot CLI instructions), see [`docs/agent-integration.md`](./docs/agent-integration.md).
For cutting a release, see [`docs/RELEASING.md`](./docs/RELEASING.md).

### Discovering commands

Every group and leaf command has `--help` with a description and worked
examples:

```bash
blumkin --help                     # top-level map, common workflows, exit codes
blumkin calendar --help            # a group's commands + typical flows
blumkin calendar create --help     # one command: args, notes, example invocations
```

`blumkin skills list --json` is the machine-readable catalog for agents.

### Shell completion

`blumkin completion <bash|zsh|fish>` prints a completion script. Install it once:

```bash
# bash
blumkin completion bash > ~/.blumkin-complete.bash
echo 'source ~/.blumkin-complete.bash' >> ~/.bashrc

# zsh
blumkin completion zsh > ~/.blumkin-complete.zsh
echo 'source ~/.blumkin-complete.zsh' >> ~/.zshrc

# fish
blumkin completion fish > ~/.config/fish/completions/blumkin.fish
```

Open a new shell afterwards. The script calls back into `blumkin` for
completions, so keep it on `PATH`.

## Config (`~/.config/blumkin/`)

Create `~/.config/blumkin/config.toml` (mode `0600`). Prefer **named profiles**
in one file (see `blumkin profiles list --json` and `--profile`):

```toml
default_profile = "work"

[profiles.work]
client_id = "<entra-public-client-id>"
tenant_id = "<your-entra-tenant>"
default_tz = "<IANA timezone, e.g. America/New_York>"
provider = "microsoft"
tags = ["@work", "work", "microsoft", "m365"]

[profiles.personal]
provider = "google"
default_tz = "<IANA timezone>"
google_oauth_client_file = "~/path/to/google-oauth-desktop-client.json"
tags = ["@personal", "personal", "google", "gmail"]
```

Legacy flat keys (no `[profiles.*]`) still load as one implicit profile named
`default`, with token files in the config dir root.

Set `tenant_id`, `default_tz`, and `provider` in the profile table (there are no
org-specific code defaults). `provider` defaults to `microsoft` when omitted.

Interactive browser auth is public-client only (`client_id`; plus `tenant_id` for
Microsoft). Do not set a client secret for these flows.

Microsoft token cache files (under `profiles/<name>/`, or config dir root for
legacy):

- `msal_token_cache.json`
- `auth_record.json`

### Google Workspace (`provider = "google"`)

**Full walkthrough:** [`docs/google-setup.md`](./docs/google-setup.md)
(Console project, APIs, consent screen / test users, Desktop client JSON,
named profile, login, smoke, troubleshooting).

Short form — point the profile at your Google Cloud **Desktop** OAuth client
JSON (the Console download). That file holds `client_id` / `client_secret`; do
not put the secret in toml or environment variables. **Download that JSON when
you create the client** — the secret is shown once and Google will not let you
re-download it later (recovery means Reset secret / a new client; see
[`docs/google-setup.md`](./docs/google-setup.md) §A.4):

```toml
[profiles.personal]
provider = "google"
default_tz = "..."
google_oauth_client_file = "~/path/to/google-oauth-desktop-client.json"
tags = ["@personal", "personal", "google", "gmail"]
```

`blumkin auth login` records the signed-in address as `email = "..."` in that
profile the first time (display only - it is never used to pick a profile, and
never rewritten afterwards). `blumkin profiles list` shows it, so two profiles
are tellable apart at a glance; `blumkin doctor` warns if the profile is later
signed in as a different account.

Optional: set `client_id` in toml as well; when omitted it is read from the JSON.
Keep the client JSON mode `0600` and outside the repo.

Supported Google verbs: auth, calendar `today` / `view` / `freebusy` / `suggest`
/ `create` / `accept` / `cancel` / `update` (`update` attaches a Meet link), mail `inbox` / `list` / `get` / `folders` / `attachments` (list +
`download`), and mail writes `draft` / `update-draft` / `delete-draft` /
`send-draft` / `reply` / `forward`, and `people resolve` (own contacts plus the
Workspace directory; a consumer account just uses contacts), and chat `find` /
`last` / `send` / `edit` / `delete` / `attachments` (a 1:1 is a Chat space;
Drive-backed attachments are listed but not downloadable). Everything else
fails closed with a clear
error. `mail folders` lists Gmail labels that act as folders. Mail writes use the
`gmail.compose` scope — re-run `blumkin auth login` once after upgrading, or those
calls exit `4` (`missing_scope`).

Token file (written by `blumkin auth login`): `profiles/<name>/google_token.json`.

Select a profile with `--profile` / `BLUMKIN_PROFILE` (name or unique tag). Use
`BLUMKIN_CONFIG_DIR` only to select a config **directory**. Never commit these
files. Optional `graph_timeout_seconds` in toml also bounds Google HTTP /
token-refresh calls (same knob as Microsoft Graph).

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
- [`docs/RELEASING.md`](./docs/RELEASING.md) — release flow, PyPI trusted publishing, verifying a published release  
- [`docs/google-setup.md`](./docs/google-setup.md) — Google Cloud Desktop OAuth + blumkin Google profile  
- [`.cursor/skills/blumkin/SKILL.md`](./.cursor/skills/blumkin/SKILL.md) — Cursor agent skill  

## License

MIT — see [`LICENSE`](./LICENSE).
