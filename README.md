# blumkin

Personal **Microsoft 365 and Google Workspace** skills CLI — named after Rose “Mrs. B” Blumkin, Berkshire’s legendary operator.

blumkin turns your own calendar, mail, and chat into **small, invokable skills** any coding agent (**Cursor**, **GitHub Copilot**, **Claude**, …) — or a human — can run over the shell, instead of re-implementing OAuth and the Graph / Google API clients each time.

It acts **as the signed-in user** over **delegated** OAuth (public client + interactive browser). No server, no app-only permissions, no shared secret. `--json` on every command for agents.

## What it does

One config file, one or more **named profiles** (Microsoft, Google, or both), selected with `--profile` or a tag. 38 skills (`blumkin skills list --json`):

| Group | Skills |
|-------|--------|
| `auth` | `login`, `logout`, `refresh`, `status` |
| `calendar` | `today`, `view`, `freebusy`, `suggest`, `create`, `accept`, `cancel`, `update` |
| `mail` | `inbox`, `list`, `get`, `folders`, `draft`, `update-draft`, `delete-draft`, `send-draft`, `reply`, `forward`, `signature`, `attachments` (+ `download`) |
| `chat` (Teams / Google Chat) | `find`, `last`, `send`, `edit`, `delete`, `attachments` (+ `download`) |
| `meeting` (Microsoft) | `get`, `transcription` |
| `people` | `resolve` |

Plus `blumkin doctor` (setup check), `skills` / `profiles` (discovery), `upgrade` (self-update over pipx), and `completion`.

Reads work with the base scope set. Anything that reaches another person (mail send, calendar invite, chat message) needs an explicit `--yes`. Google support is at near-parity with Microsoft — the **Google Workspace** section below has the exact verb list and the handful of provider differences.

## Install (`blumkin` on `PATH`)

```bash
pipx install blumkin
pipx ensurepath          # first pipx install only; opens a new shell
```

Then invoke the binary directly — **not** `uv run blumkin`:

```bash
blumkin --version                       # version, commit, and which binary answered
blumkin auth login                      # once per machine / when cache is cold
blumkin doctor                          # config, token cache, active scope set
blumkin skills list --json              # machine-readable catalog for agents
blumkin calendar today --json
blumkin mail inbox --top 10 --json
blumkin chat last --with "Sam Rivera" --n 3 --json
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

Create `~/.config/blumkin/config.toml` (mode `0600`). One file, **named
profiles** — one per account, Microsoft or Google — selected with `--profile`,
a `tags` entry, or `BLUMKIN_PROFILE` (see `blumkin profiles list --json`). With
more than one profile, blumkin fails closed rather than guessing which account
to act as.

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

`BLUMKIN_CONFIG_DIR` overrides the config **directory** (not a profile) — useful
for an isolated setup or tests. Never commit anything under the config dir.

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

**Coverage.** Google runs `auth`, all of `calendar` (`update` attaches a Meet
link instead of a Teams link), all of `mail` reads and writes, `people resolve`
(own contacts, plus the Workspace directory on a Workspace account), and `chat`
`find` / `last` / `send` / `edit` / `delete` / `attachments`. Everything else
fails closed with a clear error.

**Provider differences.**

- `mail folders` lists Gmail labels that act as folders; `mail list --folder`
  still takes the well-known names.
- Mail writes need the `gmail.compose` scope — re-run `blumkin auth login` once
  after upgrading or those calls exit `4` (`missing_scope`).
- Chat `attachments` are listed but Drive-backed files are not downloadable.
- A returned draft `id` is the Gmail draft id; `attachments[].id` is `null`
  (Gmail carries attachments inside the raw message).

**Token file** (written by `blumkin auth login`):
`profiles/<name>/google_token.json`. `graph_timeout_seconds` in toml bounds
Google HTTP and token-refresh calls too. Never commit any of these files.

## Tests

```bash
uv run pytest -m 'not live'          # CI-equivalent (mocks / offline)
BLUMKIN_LIVE=1 uv run pytest -m live # live Graph reads + silent refresh
```

Live tests need `~/.config/blumkin/` by default (override with `BLUMKIN_CONFIG_DIR`):
`config.toml`, token cache, auth record, and a usable refresh token. Never commit those files.

## Security

blumkin acts as **you** over delegated OAuth — no server, no shared secret, no
one else's data. Auth and config live only under `~/.config/blumkin/` and are
never committed.

- **[`docs/SECURITY-AT-A-GLANCE.md`](./docs/SECURITY-AT-A-GLANCE.md)** — one page: what it touches, the auth model, blast radius, how releases are trusted.
- [`SECURITY.md`](./SECURITY.md) — full policy, response targets, and private vulnerability reporting.

## Docs

- [`PLAN.md`](./PLAN.md) — CLI design  
- [`docs/DECISIONS.md`](./docs/DECISIONS.md) — standing decisions and the design-artifact index  
- [`CONTRIBUTING.md`](./CONTRIBUTING.md) — review model and the static-analysis / scanning toolchain  
- [`HANDOFF.md`](./HANDOFF.md) — session handoff  
- [`AGENTS.md`](./AGENTS.md) — contributor / agent ground rules  
- [`RETROSPECTIVE-M1.md`](./RETROSPECTIVE-M1.md) — M1 ship retrospective (#11)  
- [`docs/agent-integration.md`](./docs/agent-integration.md) — using blumkin from Cursor / Copilot CLI, and the frozen `skills list --json` contract  
- [`docs/RELEASING.md`](./docs/RELEASING.md) — release flow, PyPI trusted publishing, rollback  
- [`docs/google-setup.md`](./docs/google-setup.md) — Google Cloud Desktop OAuth + blumkin Google profile  
- [`.cursor/skills/blumkin/SKILL.md`](./.cursor/skills/blumkin/SKILL.md) — Cursor agent skill  

## License

MIT — see [`LICENSE`](./LICENSE).
