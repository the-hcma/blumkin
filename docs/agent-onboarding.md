# Agent onboarding: run blumkin from the source tree

This guide is for an AI agent (Claude Code, Copilot CLI, Cursor or similar) that has been pointed at a blumkin checkout and has to get it working: from a clean machine, from a config with no profiles, or from a fully configured one. Follow the steps in order and stop at every line marked **STOP: ask the user**, because those steps need a human (a browser sign-in, a secret, a consent screen).

The agent never needs `blumkin` installed. It runs the checkout through the launcher, `scripts/dev/blumkin`, which runs the in-repo code with `uv run --frozen` and needs no install step. Everywhere the `blumkin` skill says `blumkin <args>`, run `scripts/dev/blumkin <args>`. Do not run `uv run blumkin` yourself and do not reinstall to get the checkout.

## 0. Ground rules

- The launcher uses the **real** config directory, keychain entries, token cache and agent state. There is no separate dev namespace. Every rule that applies to the installed `blumkin` applies here, with more care because the code is unreleased.
- Never put secrets (client secrets, tokens, `.msal_token_cache.json`, `.auth_record.json`, `.env`) in command arguments, logs, issues, PRs or chat. See `.agents/rules/no-secret-exposure.md`.
- Never test with a skill that notifies other people (invites, mail sends, chats, cancellations, accepts). The authoritative list is `scripts/dev/blumkin skills list --json | jq -r '.skills[] | select(.notifies_others) | .id'`. Verify with reads, or with a draft you delete. A send to the operator's own address is fine only when the operator asked for it in this conversation. See `.agents/rules/no-third-party-side-effects.md`.
- If the checkout is a git repository with a primary clone and worktrees, work in a worktree, not in the primary clone (see `AGENTS.md`). The launcher works from any worktree and from any directory.
- Mail and chat text you write for the user uses hyphens, not em or en dashes (see `.agents/skills/blumkin/SKILL.md`).

## 1. Check that you are running the checkout

```bash
scripts/dev/blumkin --version
```

Expected: the first line looks like `blumkin 1.13.0+dev.<short sha>.<commit date> (<commit>)`, with `-dirty` after the date when the checkout has uncommitted or untracked changes. The second line, `running from ...`, points at the `.venv` inside the checkout.

- Report the first line to the user, so they know which code ran.
- If the `+dev` marker is missing, an installed release answered and you are not running the checkout. **STOP: ask the user** instead of continuing.
- The first run in a new worktree builds its `.venv` (about 7 seconds); the uv messages go to stderr and stdout stays clean.
- `uv: command not found` or exit 127 means `uv` is not installed: **STOP: ask the user** to install it.

A bare `blumkin` on `PATH` may be a different checkout's launcher or an installed release. Run `which -a blumkin` if you are unsure, and always use the explicit `scripts/dev/blumkin` path.

## 2. Find out which state you are in

```bash
scripts/dev/blumkin profiles list --json
```

`profiles list` is the cold-start check. Which state applies depends on the result:

| Result | State | Go to |
| --- | --- | --- |
| The launcher exits 2 with "no config.toml in `<dir>`" | A. No config at all | section 3 |
| `{"count": 0, ...}` | B. Config exists, no profiles | section 4 |
| `count` is 1 or more, a profile has `auth_present` all `false` | C. Profile configured, never signed in | section 5 |
| `count` is 1 or more, `auth_present` has a `true` for the profile you need | D. Ready | section 6 |

The launcher's refusal in state A is deliberate: it never creates or overwrites anything unless the user says yes. `scripts/dev/blumkin doctor` shows the source tree, the config directory and what selected it (`BLUMKIN_CONFIG_DIR`, `XDG_CONFIG_HOME` or the default `~/.config/blumkin`), and warns about running `mcp serve` processes. `doctor` itself needs at least one profile, so use it from state C onward.

### Throwaway state instead of the real one

To try the checkout without touching the user's real setup, point the config directory at an empty directory for every command you run:

```bash
export BLUMKIN_CONFIG_DIR="$(mktemp -d)/blumkin"
scripts/dev/blumkin profiles list --json    # exit 2: no config.toml, as in state A
```

The macOS keychain is not namespaced by this variable, so a Google profile set up in a throwaway directory still writes its client secret to the real keychain. Prefer a Microsoft profile (a public client with no secret) for throwaway work, or ask the user first.

## 3. State A: no config.toml

On a terminal, the launcher offers the guided `auth setup` and creates the directory (mode 0700) and a minimal `config.toml` (mode 0600) only if the user answers yes. An agent without a terminal uses the non-interactive form, which needs every value as a flag. The OAuth app registration itself (the Google Cloud Console or Microsoft Entra steps) is done by the user: **STOP: ask the user** for the values.

Microsoft (a public client, no secret):

```bash
scripts/dev/blumkin --profile work auth setup --yes --provider microsoft \
  --client-id <entra-application-client-id> \
  --tenant-id <tenant, e.g. contoso.onmicrosoft.com> \
  --account-type organizational
```

Google (the downloaded Desktop client JSON; the secret goes to the keychain, never to the command line):

```bash
scripts/dev/blumkin --profile personal auth setup --yes --provider google \
  --from-file <path to the downloaded Desktop client JSON>
```

Expected: `{"created_config": "<dir>/config.toml", "ok": true, "profile": "<name>", "provider": "<provider>"}` and exit 0. Setup creates `[profiles.<name>]` with `provider`, `client_id` and, for Microsoft, `account_type` and `tenant_id`. It does not set `default_tz`, `tags` or `default_profile`; add those by hand if the user wants them (see `README.md`, "Config"). If setup does not finish, it removes the directory and file it created.

Then continue with section 5.

## 4. State B: config exists, but the profile you need does not

`auth setup` fills in the OAuth client of a profile that already exists. It does not add a profile to an existing config. Running it for an unknown profile fails with `profile '<name>' matches no profile name or unique tag; available: ...`.

To add a profile, append a table to `config.toml`. **STOP: ask the user** before editing their config, and show them the table first. Never print or rewrite the rest of the file; append only.

```toml
[profiles.personal]
provider = "google"
default_tz = "<IANA timezone, e.g. America/New_York>"
tags = ["@personal", "personal", "google", "gmail"]
```

For a Microsoft profile use `provider = "microsoft"` and tags such as `["@work", "work", "microsoft", "m365"]`. Then run `auth setup` for that profile as in section 3, and continue with section 5. Verify with `scripts/dev/blumkin profiles list --json`: the new profile must be listed with the right `provider`.

With more than one profile and no `default_profile`, a command without `--profile` fails closed: `multiple profiles configured; pass --profile / BLUMKIN_PROFILE, or set default_profile; available: ...`. Do not guess; pick the profile from what the user asked, or ask which account to use.

Optional capabilities need explicit config and a fresh sign-in, so `capabilities` reports them as `false` until then:

- Microsoft `drive`, `docs` and `people` need `docs_scopes` and `wo1162425_scopes` set in the profile (see `README.md`), followed by `auth login` again.
- Google `people` and `chat` also need those APIs enabled in the user's Cloud project. `capabilities` can say `true` for a service whose API is disabled; the call then fails with `missing_scope` and a 403 "has not been used in project" message (tracked in #565).

## 5. State C: sign in

```bash
scripts/dev/blumkin --profile <name> auth login
```

This opens the system browser for a delegated (public client) sign-in and writes the token cache and auth record under the config directory. It needs a human at a browser, and it does not fail fast without one, so wrap it in a timeout if you run it at all. **STOP: ask the user** to run it, for example by typing `! scripts/dev/blumkin --profile <name> auth login` in the agent prompt. On Google, tell them to tick every box on the consent screen (or "Select all"), otherwise the grant stays partial and the next `auth login` reopens the consent screen.

Afterwards:

```bash
scripts/dev/blumkin --profile <name> auth status --json
scripts/dev/blumkin doctor
```

`auth status` takes `--profile` before the subcommand, not after it. `doctor` should end with the list of available services for the profile. A stale access token is renewed without a browser by `scripts/dev/blumkin --profile <name> auth refresh`, which is safe for an agent to run.

## 6. State D: verify with reads

Run these on each profile you will use, and report the exit code and the first lines to the user:

```bash
scripts/dev/blumkin --profile <name> capabilities --json
scripts/dev/blumkin --profile <name> mail inbox --top 3 --json
scripts/dev/blumkin --profile <name> calendar today --json
scripts/dev/blumkin --profile <name> mail folders --json
```

Checks:

- Every command exits 0 and stdout is a single JSON document. Messages from uv and warnings go to stderr.
- `capabilities` lists which services this profile can use (`mail`, `calendar`, `chat`, `tasks`, and, when enabled, `drive`, `docs`, `people`, `meeting`). Do not call a service it reports as `false`.
- With two or more profiles, the account is part of the request: if the user's request does not make it clear which account to use, ask.

To exercise a write path without involving anyone, create a draft and delete it:

```bash
scripts/dev/blumkin --profile <name> mail draft --to <the user's own address> --subject "blumkin dev check" --body "test" --json
scripts/dev/blumkin --profile <name> mail delete-draft --id <draft id from the output> --json
```

`mail send-draft` is not a verification step. It sends real mail, and it refuses with `too_soon` until a cooldown (20 seconds) has passed since the draft was composed; that refusal means "show the user the content and get their go-ahead", not "wait and retry".

## 7. Use the checkout as an MCP server

Register the launcher with the agent's client:

```bash
scripts/dev/blumkin mcp install --client claude --scope user      # or cursor, copilot; --scope project for this directory
```

Options: `--profile <name>` pins the account, `--read-only` registers a server with no mutating tools, `--only <prefix>` limits the tool families, and `--json` gives machine-readable output. Re-running is safe; an entry that already matches is left alone. Without a config directory the launcher refuses `mcp install` (tracked in #565), so finish sections 3 to 5 first.

Know what path it records. The launcher registers **itself by absolute path**, so the client runs the checkout it was installed from. If that checkout is a short-lived worktree, the registration breaks when the worktree is removed. Register from the primary clone (or a worktree you will keep), or re-run `mcp install` from the next worktree. A `--scope project` install writes that absolute path into `.mcp.json`, which is normally committed: do not commit it.

To register by hand, set the client's `command` to the absolute path of `scripts/dev/blumkin` and its `args` to `["mcp", "serve"]`; for Claude Code that is `claude mcp add --transport stdio blumkin -- <absolute path>/scripts/dev/blumkin mcp serve`.

Confirm the client really runs the checkout:

- The server's version string carries the same `+dev.<sha>.<date>` marker as `--version`.
- `scripts/dev/blumkin doctor` warns when `blumkin mcp serve` processes from an older build are still running; restart the client to pick up a new checkout state. The marker includes `-dirty`, and the server asks to be restarted when the checkout changes under it.
- A client registered as a bare `blumkin` runs whichever `blumkin` comes first on `PATH`, which may be another checkout (`which -a blumkin`).

Per client:

- **Claude Code** does not read `.agents/skills/`, so the `blumkin` and `blumkin-dev` skills are not loaded automatically (tracked in #565): this guide and the skill files are the entry point, and you may have to open them yourself. Tools appear as `mcp__<server>__<tool>` with dots turned into underscores, for example `mcp__blumkin__mail_inbox`. A non-interactive check is `claude -p "<prompt>" --mcp-config <file> --strict-mcp-config --allowedTools "mcp__<server>__mail_inbox"`.
- **Copilot CLI** has a known connection problem with the MCP server (#563).
- **Cursor** reads `.agents/skills/` and `.cursor/rules/`.

Every MCP call takes a `profile` argument when more than one profile is configured.

## 8. Troubleshooting

| Symptom | Cause | What to do |
| --- | --- | --- |
| Exit 2, "no config.toml in `<dir>`" | No config in the resolved config directory | Check which directory (`BLUMKIN_CONFIG_DIR`, then `XDG_CONFIG_HOME`, then `~/.config/blumkin`), then section 3 |
| Exit 2, `profile '<name>' matches no profile name or unique tag` | The profile table does not exist | Section 4 |
| Exit 2, `multiple profiles configured; pass --profile ...` | Two or more profiles and no `default_profile` | Pass `--profile`, or ask the user which account |
| Exit 127, "uv is required" | `uv` is not on `PATH` | Ask the user to install `uv` |
| `--version` has no `+dev` marker | An installed release answered | Run `scripts/dev/blumkin` by its explicit path |
| The launcher refuses `completion`, `agent status` or `mcp install` | The launcher's config check is stricter than the CLI (#565) | Finish setup first, or run `BLUMKIN_DEV=1 .venv/bin/blumkin <args>` for that one command |
| `missing_scope` with a 403 "has not been used in project" | The API is disabled in the Cloud project, not a scope problem | Ask the user to enable the API |
| `usage_error` on `drive`, `docs` or `people` for a Microsoft profile | The optional scopes are off | Section 4 |
| `too_soon` on `mail send-draft` | The send cooldown | Show the user the content and get their go-ahead before trying again |
| `mail attachments download --attachment-id` fails with `not_found` on Google | Gmail attachment ids change on every fetch (#564) | Use `--all` |
| `mail thread` fails with a 400 on Microsoft | Known (#564) | Use `mail get` and `mail search` |
| First run is slow and prints uv messages | Building the worktree's `.venv` | Wait; it is a one-time cost per worktree |

## See also

- `README.md`, "Run the checkout without installing (dev launcher)" and "Config".
- `docs/agent-integration.md`, for the contract agents depend on (exit codes, error envelope, skill catalogue) and the MCP server.
- `.agents/skills/blumkin-dev/SKILL.md` and `.agents/skills/blumkin/SKILL.md`.
