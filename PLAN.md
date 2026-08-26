# Blumkin — CLI plan (review)

Personal Microsoft 365 / Graph **skills CLI**. Named after Rose “Mrs. B” Blumkin.

**Language decision:** **Python 3.14+** with **`uv`** for packaging and local gates (Blumkin is a Graph skills CLI, not a Bot Framework host). End users and agents invoke **`blumkin` on `PATH`**, not `uv run blumkin`.

Blumkin = delegated Graph access **as the signed-in user**.

---

## 1. Problem & north star

Agents re-implement Graph auth and one-off scripts. Blumkin exposes proven flows as **stable skills** any agent can shell.

```text
Agent (Cursor / Copilot / Claude / …)
    →  blumkin <group> <action> [args] [--json]
    →  Microsoft Graph (delegated, signed-in user)
    →  stdout (+ exit code)
```

| Property | Rule |
|----------|------|
| Exit codes | `0` success; `2` usage; `3` auth; `4` missing scope / Graph forbidden; `5` not found; `1` other |
| Output | Human text default; **`--json`** for agents (single JSON object or NDJSON where noted) |
| Auth | Cached refresh + `AuthenticationRecord`; browser only when required |
| Safety | Read-looking names never send/cancel; writes need explicit verbs + `--yes` when notifying others |

---

## 2. Non-goals (v1)

- Hosting a Teams bot / Bot Framework messaging endpoint  
- App-only Graph (`Mail.Send` application, `Calendars.ReadWrite.All`, …)  
- Full MCP server as the **primary** integration (optional thin wrapper later only)  
- GUI / TUI  
- Multi-tenant SaaS  

---

## 3. CLI surface (what it will look like)

### 3.1 Invocation

```bash
blumkin [--json] [-v|--verbose] [--tz AREA] <command> ...
```

| Global flag | Meaning |
|-------------|---------|
| `--json` | Machine output on stdout; diagnostics on stderr |
| `-v`, `--verbose` | Log request labels (never tokens) on stderr |
| `--tz AREA` | Default `America/New_York`; used for display + parsing local times |

Install (planned): put `blumkin` on `PATH` via `uv tool install` (or `pipx` / equivalent). Dev checkouts use `uv sync`; do **not** document `uv run blumkin` as the product interface.

### 3.2 Command tree

```text
blumkin
├── auth
│   ├── login
│   ├── status
│   └── logout
├── skills
│   ├── list
│   └── describe <skill-id>
├── doctor
├── calendar
│   ├── today
│   ├── view
│   ├── freebusy
│   ├── accept
│   ├── create
│   └── cancel
├── mail
│   ├── inbox
│   ├── draft
│   ├── delete-draft
│   ├── update-draft
│   └── send-draft
├── chat
│   ├── find
│   ├── last
│   ├── send          # needs Chat.ReadWrite
│   ├── edit          # needs Chat.ReadWrite
│   └── delete        # needs Chat.ReadWrite
└── meeting
    ├── get
    └── transcription # needs OnlineMeetings.ReadWrite
```

Help: every group and leaf supports `-h` / `--help`.

### 3.3 Auth commands

```bash
blumkin auth login          # interactive browser; write cache + auth record
blumkin auth status         # expiry, scopes present, cache paths
blumkin auth status --json
blumkin auth logout         # delete cache files
```

**Config (no secrets in git):**

| Source | Keys |
|--------|------|
| Env | `BLUMKIN_CLIENT_ID`, `BLUMKIN_TENANT_ID` (default `brk.tech`) |
| File | `~/.config/blumkin/config.toml` or `./.blumkin.toml` (gitignored locally) |
| Cache | `~/.config/blumkin/msal_token_cache.json`, `auth_record.json` (XDG); optional project override |

Reuse proven pattern: `InteractiveBrowserCredential` + `SerializableTokenCache` + `AuthenticationRecord` (both caches required for silent auth).

### 3.4 Meta / agent discovery

```bash
blumkin skills list
blumkin skills list --json
blumkin skills describe calendar.today --json
blumkin doctor              # config, cache, scopes vs skill requirements
```

**`skills list --json` schema (v1 draft):**

```json
{
  "version": 1,
  "cli": "blumkin",
  "skills": [
    {
      "id": "calendar.today",
      "cli": ["blumkin", "calendar", "today"],
      "summary": "List the signed-in user's events for today",
      "mutates": false,
      "notifies_others": false,
      "scopes": ["Calendars.ReadWrite"],
      "args": [
        {"name": "--date", "type": "date", "required": false},
        {"name": "--tz", "type": "iana_tz", "required": false}
      ]
    }
  ]
}
```

Agents should prefer this catalog over inventing Graph calls.

### 3.5 Calendar

```bash
# Read
blumkin calendar today
blumkin calendar today --json
blumkin calendar view --from 2026-08-25 --to 2026-08-26
blumkin calendar freebusy --with vivek.haldar@brk.tech \
  --start "2026-08-27T17:00" --end "2026-08-27T17:30"

# Write (notifies attendees when applicable)
blumkin calendar accept --today-pending --yes
blumkin calendar accept --event-id '<id>' --yes
blumkin calendar create \
  --subject "Henrique / Vivek" \
  --with vivek.haldar@brk.tech \
  --start "2026-08-26T11:00" \
  --duration 30m \
  --teams \
  --yes
blumkin calendar cancel --event-id '<id>' --yes
```

**Rules:**
- Times without `Z` are interpreted in `--tz` (default America/New_York).  
- Graph UTC `dateTime` always converted for display (bug we already fixed).  
- `--yes` required for accept/create/cancel.  
- `create --teams` sets online meeting provider Teams.

**JSON event object (sketch):**

```json
{
  "id": "...",
  "subject": "...",
  "start": "2026-08-25T11:00:00-04:00",
  "end": "2026-08-25T11:30:00-04:00",
  "timezone": "America/New_York",
  "location": "Microsoft Teams Meeting",
  "organizer": {"name": "...", "email": "..."},
  "is_organizer": false,
  "response": "notResponded",
  "online_join_url": "https://teams.microsoft.com/..."
}
```

### 3.6 Mail

```bash
blumkin mail inbox --top 10
blumkin mail inbox --top 10 --json
blumkin mail draft --to hcma@hcma.info --subject "hello you!" --body "hello you!"
blumkin mail draft --to hcma@hcma.info --subject "html" --body "<p>Hi</p>" --body-type html
blumkin mail draft --to hcma@hcma.info --subject "from file" --body-file ./message.html --body-type html
blumkin mail delete-draft --id '<draft-id>'
blumkin mail update-draft --id '<draft-id>' --subject "revised" --body-file ./message.html --body-type html
blumkin mail send-draft --id '<draft-id>' --yes
```

- Default: create **draft** only (safe).  
- `--body` and `--body-file` are mutually exclusive; `--body-type` is `text` (default) or `html`.  
- `delete-draft` / `update-draft` do not require `--yes` (no recipient notify).  
- Sending always `--yes`.  
- Optional later: `mail send` one-shot (still `--yes`).

### 3.7 Chat

```bash
blumkin chat find --with "Daniel Erickson"
blumkin chat last --with "Daniel Erickson" --n 3
blumkin chat last --with "David McKenzie" --n 1 --json

# After Chat.ReadWrite follow-up is granted (and re-consent):
blumkin chat send --with "…" --text "…" --yes
blumkin chat edit --chat-id … --message-id … --text "…" --yes
blumkin chat delete --chat-id … --message-id … --yes
```

- `--with` matches display name (case-insensitive) preferring 1:1 chats.  
- Strip HTML in human mode; JSON may include `body_text` + `body_html`.  
- Commands are implemented; **live** Graph success still needs Identity grant + re-login (see Phase 4 TODO).

### 3.8 Meetings

```bash
blumkin meeting get --event-id '<id>'
blumkin meeting transcription --event-id '<id>'          # show flags
blumkin meeting transcription --event-id '<id>' --enable --yes
```

Implemented against Graph; **live** enablement needs `OnlineMeetings.ReadWrite` on the Entra app + re-consent.

---

## 4. Skill inventory (IDs stable)

| Skill ID | CLI | Mutates | Notifies others | Scopes | Proven? |
|----------|-----|---------|-----------------|--------|---------|
| `auth.login` | `auth login` | cache | no | (sign-in) | yes |
| `auth.status` | `auth status` | no | no | — | yes |
| `auth.logout` | `auth logout` | cache | no | — | — |
| `skills.list` | `skills list` | no | no | — | — |
| `skills.describe` | `skills describe` | no | no | — | — |
| `doctor` | `doctor` | no | no | — | — |
| `calendar.today` | `calendar today` | no | no | Calendars.* | yes |
| `calendar.view` | `calendar view` | no | no | Calendars.* | partial |
| `calendar.freebusy` | `calendar freebusy` | no | no | Calendars.* | yes |
| `calendar.accept` | `calendar accept` | yes | yes | Calendars.ReadWrite | yes |
| `calendar.create` | `calendar create` | yes | yes | Calendars.ReadWrite | yes |
| `calendar.cancel` | `calendar cancel` | yes | yes | Calendars.ReadWrite | yes |
| `mail.inbox` | `mail inbox` | no | no | Mail.Read* | yes |
| `mail.delete-draft` | `mail delete-draft` | yes | no | Mail.ReadWrite | yes |
| `mail.draft` | `mail draft` | yes | no | Mail.ReadWrite | yes |
| `mail.send-draft` | `mail send-draft` | yes | yes | Mail.Send | yes |
| `mail.update-draft` | `mail update-draft` | yes | no | Mail.ReadWrite | yes |
| `chat.find` | `chat find` | no | no | Chat.ReadWrite | yes |
| `chat.last` | `chat last` | no | no | Chat.ReadWrite | yes |
| `chat.send` | `chat send` | yes | yes | Chat.ReadWrite | mocked; live pending Identity |
| `chat.edit` | `chat edit` | yes | yes | Chat.ReadWrite | mocked; live pending Identity |
| `chat.delete` | `chat delete` | yes | yes | Chat.ReadWrite | mocked; live pending Identity |
| `meeting.get` | `meeting get` | no | no | Calendars + OnlineMeetings | mocked; live pending Identity |
| `meeting.transcription` | `meeting transcription` | yes if `--enable` | no* | OnlineMeetings.ReadWrite | mocked; live pending Identity |

\*Enabling transcription does not email attendees by itself.

**Semver:** skill **IDs** and `--json` field names are semver’d with the CLI; additive fields OK, renames/removals = major.

---

## 5. Output & errors

### Human mode
- Tables / bullet lines; times in local TZ with abbreviation (`EDT`).  
- Secrets never printed (tokens, client secret).

### JSON mode
- Success: one JSON document (or `{ "items": [ ... ] }`).  
- Failure: still exit non-zero; stderr message; optional `--json` error object:

```json
{ "ok": false, "error": "auth_required", "message": "Run: blumkin auth login", "hint": "…" }
```

### Agent contract (summary)
1. `blumkin skills list --json`  
2. Run skill with `--json`  
3. On `auth_required` / exit `3` → ask user to `blumkin auth login`  
4. On `missing_scope` / exit `4` → point at private-lab Identity follow-up notes (not public WO numbers)  

Full integration guidance (skill vs MCP, Cursor / Copilot CLIs): **§6**.

---

## 6. Agent integration — Cursor Agent CLI & Copilot CLI

**Target UX:** from a terminal agent session (`cursor agent` / Cursor CLI, or **GitHub Copilot CLI**), say “what’s on my calendar today?” and the agent runs Blumkin — not raw Graph SDK code.

### 6.1 Skill vs MCP — recommendation

| Approach | What it is | Pros | Cons |
|----------|------------|------|------|
| **Agent Skill** (+ shell) | Markdown skill (`SKILL.md`) teaches *when/how* to invoke `blumkin … --json` | One implementation (the CLI); works anywhere with a shell; easy to version in-repo or personal skills dir; matches how both CLIs already run tools | Model must follow instructions; discovery = skill + `skills list` |
| **MCP server** | Process exposing tools over MCP; each Blumkin verb ≈ a tool | Native tool schemas in MCP-aware UIs; structured args without parsing `--help` | Second surface to maintain; another process/lifecycle; still needs auth/cache; Cursor/Copilot CLI shell path is already enough for v1 |

**Decision for v1: Skill + shell, not MCP.**

Reasons:
1. Blumkin **is** already the tool surface (`skills list --json` is the catalog).  
2. **Cursor Agent CLI** and **Copilot CLI** both excel at running shell commands; a skill that says “prefer `blumkin`” is the lightest glue.  
3. MCP duplicates every command and drifts unless it only shells to Blumkin (then MCP is pure overhead for CLI-first agents).  
4. Auth (browser / Keychain / token files) fits a local CLI better than a long-lived MCP daemon in early versions.

**Optional later (Phase 5+):** a **thin MCP adapter** that only wraps `blumkin … --json` if an IDE/UI wants MCP tool cards — same CLI remains source of truth.

```text
Preferred (v1):

  Copilot CLI / Cursor Agent CLI
       │  (reads Blumkin skill / instructions)
       ▼
  shell: blumkin calendar today --json
       ▼
  Graph (delegated)

Optional later:

  MCP host  →  blumkin-mcp  →  shell/exec blumkin … --json  →  Graph
```

### 6.2 Cursor Agent CLI

| Piece | Role |
|-------|------|
| **CLI binary** | `blumkin` on `PATH` (e.g. `uv tool install`) |
| **Skill** | Project skill `.cursor/skills/blumkin/SKILL.md` and/or personal `~/.cursor/skills/blumkin/` so *any* repo session can use it |
| **When to trigger** | Description: Microsoft 365, Outlook calendar/mail, Teams chat, free/busy, Graph “as me” |
| **Behavior** | Skill instructs: discover via `blumkin skills list --json`; always pass `--json` in agent mode; never invent Graph calls if Blumkin covers the job; require `--yes` for notify-others; on exit 3 run/login guidance |

Example skill outline (to author in Phase 5 — not code yet):

```markdown
---
name: blumkin
description: >-
  Personal Microsoft 365 via the blumkin CLI (calendar, mail, Teams chat,
  free/busy). Use when the user asks about their Outlook/Teams as themselves.
---
# Blumkin

1. Prefer shelling to `blumkin` over writing Graph/SDK code.
2. Run `blumkin skills list --json` if unsure which command exists.
3. Always use `--json` for machine parsing.
4. Writes that email or invite others require `--yes`.
5. Auth errors → tell user to run `blumkin auth login` on this machine.
```

Cursor Agent CLI then uses its normal **shell tool** — no MCP required.

Also useful: a short note in **user rules** or repo `AGENTS.md`: “For personal M365 actions on this machine, use Blumkin.”

### 6.3 GitHub Copilot CLI

Same pattern:

| Piece | Role |
|-------|------|
| **CLI on PATH** | Identical `blumkin` binary |
| **Instructions** | Copilot custom instructions / local guidance: prefer Blumkin for M365 personal tasks |
| **Skill (if Copilot skills supported in your setup)** | Same `SKILL.md` content, or a Copilot-oriented copy under `.github/` / docs that points at the CLI |
| **Execution** | Copilot CLI runs `blumkin … --json` via its shell/command capability |

Do **not** depend on Copilot “knowing” Graph APIs. Depend on Blumkin’s stable CLI + `--json` contract.

Auth stays **on the machine** where the Copilot CLI runs (same token cache as Cursor). Headless CI agents without a browser need a prior `blumkin auth login` on that host (or we document device limitations).

### 6.4 What we ship for integration (phased)

| Deliverable | Phase |
|-------------|--------|
| Stable `blumkin skills list --json` + exit codes | 1–2 |
| `.cursor/skills/blumkin/SKILL.md` (project) | 5 |
| Optional install notes: personal skill symlink / copy to `~/.cursor/skills/` | 5 |
| Copilot instructions snippet in `docs/agent-integration.md` | 5 |
| MCP adapter (optional, shells to CLI only) | later if needed |

### 6.5 Anti-patterns

- Teaching the agent to call Graph with ad-hoc Python while Blumkin exists  
- MCP server that re-implements Graph separately from the CLI  
- Skills that embed secrets or client IDs  
- Auto-running `calendar create` / `mail send-draft` without `--yes` / user intent  

---

## 7. Packaging & repo layout (when code lands)

```text
blumkin/
  README.md
  PLAN.md
  AGENTS.md
  LICENSE
  pyproject.toml          # hatchling; script blumkin = …
  uv.lock
  src/blumkin/
    __init__.py
    cli.py                # argparse or typer/click
    auth.py
    config.py
    graph.py              # shared Graph client
    output.py             # text / json helpers
    skills/
      calendar.py
      mail.py
      chat.py
      meeting.py
      meta.py
  tests/
  .github/                # practices + CI (see §7)
  .cursor/rules/          # practices (already seeded)
```

Dependencies (expected): `msgraph-sdk`, `azure-identity`, `msal`, click/typer, …  
Dev: `ruff`, `pyright`, `pytest`.

---

## 8. Repository-helpers / `github-repo-lint` readiness

Blumkin ([the-hcma/blumkin](https://github.com/the-hcma/blumkin)) should comply with
[repository-helpers](https://github.com/the-hcma/repository-helpers) **github-repo-lint**
like `bunnify` / `domesti-bot`.

### Already seeded in this repo
| Artifact | Purpose |
|----------|---------|
| `AGENTS.md` | Agent ground rules (uv, ruff, gh-stack, worktrees) |
| `LICENSE` | MIT + copyright |
| `.github/CODEOWNERS` | `@thehcma` |
| `.github/stacking-tool` | `gh-stack` |
| `.cursor/rules/*.mdc` | read-agents, stacking, pr-ship, commit identity, lexicographic, repo-practices-after-config-change, main-worktree-off-limits |

### Lint from a repository-helpers clone
```bash
# in a stack worktree of this repo, or with --repo:
scripts/github-repo-lint --repo the-hcma/blumkin --suggest
scripts/github-repo-lint --repo the-hcma/blumkin --apply-fix
```

### Expected to appear via lint / apply-fix (do not hand-roll forever)
- `.github/workflows/ci.yml` with **Python lint & format checks** via `.github/ci/python-static`  
- `.github/workflows/cve-check.yml` once `uv.lock` exists  
- `.github/ci/secret-scan` (gitleaks)  
- Branch cleanup / merged-PR closer / Dependabot auto-merge as org defaults  
- `protect-main` + merge queue (GitHub settings — lint applies)  
- Session-start + stacking rules kept in sync with `repo-practices-cursor` templates  

### Local gates (after code)
```bash
# from repository-helpers:
scripts/dev/pre-pr-checks
# from this repo (dev tooling — not the product CLI):
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

### Python static CI convention
One job: **Python lint & format checks** (ruff check + format --check + pyright).  
No separate required checks named only `Ruff` / `Pyright` / `Backend Lint`.

---

## 9. Phases

### Phase 0 — Docs & practices scaffold
- [x] Repo [the-hcma/blumkin](https://github.com/the-hcma/blumkin)  
- [x] `README.md`, expanded `PLAN.md`  
- [x] `AGENTS.md`, `LICENSE`, CODEOWNERS, stacking marker, cursor rules  
- [x] Initial commit + public remote  
- [x] `github-repo-lint --strict-onboarding` (protect-main, MQ, CI hygiene)  

### Phase 1 — Skeleton CLI (M1 — shipped in [#10](https://github.com/the-hcma/blumkin/pull/10))
- [x] `pyproject.toml` + `uv.lock` + `blumkin` entrypoint on `PATH` (`uv tool install`)  
- [x] `auth login|status|logout`  
- [x] `skills list|describe`, `doctor`  
- [x] Port auth/cache pattern from the private Graph lab  
- [x] Ruff/pyright/pytest wired; `.github/ci/python-static` + hermetic pytest + secret-scan  
- [x] First skill: `calendar.today` (M1 Graph scopes: `Calendars.ReadWrite`, `User.Read`)  
- [x] Project Cursor skill: `.cursor/skills/blumkin/SKILL.md`  

### Phase 2 — Read skills
- [x] `calendar.view|freebusy` (today already shipped)  
- [x] `mail.inbox`  
- [x] `chat.find|last`  

### Phase 3 — Write skills (gated with `--yes`)
- [x] `calendar.accept|create|cancel`  
- [x] `mail.draft|send-draft`  
- [x] `mail.delete-draft`; HTML / `--body-file` on `mail draft`  
- [x] `mail.update-draft` (PATCH in place)  

### Phase 4 — Post–Identity follow-up
**Assumption:** private-lab Identity / Remedy follow-up will grant delegated add-ons (see `HANDOFF.md`).  
- [x] Implement `chat.send|edit|delete`, `meeting.get|transcription` (hermetic / mocked tests)  
- [ ] **TODO (validate live):** after grant — update Entra client scopes, wipe token cache + auth record under the effective config dir (`BLUMKIN_CONFIG_DIR`, else `$XDG_CONFIG_HOME/blumkin` if set, else `~/.config/blumkin/`), re-login, confirm consent includes `Chat.ReadWrite` + `OnlineMeetings.ReadWrite`, then smoke chat write + meeting transcription against Graph  

### Phase 5 — Agent DX (Cursor Agent CLI + Copilot CLI)
- Freeze `skills list --json` schema  
- [x] Ship `.cursor/skills/blumkin/SKILL.md` (shell-first; see §6)  
- Document personal `~/.cursor/skills/blumkin/` install + Copilot CLI custom-instructions snippet  
- **No MCP in v1**; optional thin MCP wrapper later if a host needs it  

---

## 10. Success criteria

- Cold agent (Cursor Agent CLI or Copilot CLI) follows Blumkin skill and runs `blumkin … --json` without writing Graph code  
- Cold discover: `blumkin skills list --json`  
- Warm cache: `blumkin calendar today --json` with **no** browser  
- Calendar times correct in local TZ  
- No notify-others action without `--yes`  
- `github-repo-lint --suggest` clean (or only GitHub-settings TODOs) once remote exists  
- `pre-pr-checks` green on Python static + tests + secret-scan  

---

## 11. Open questions (for your review)

1. **Config path:** XDG `~/.config/blumkin/` only, or also allow repo-local `.blumkin.toml`?  
2. **People resolve:** keep `--with "Display Name"` fuzzy match, or require email once `People.Read` lands?  
3. **Migrate private Graph lab:** leave as lab until Blumkin Phase 2–3, then archive?  
4. **Default duration** for `calendar create` if `--duration` omitted (propose `30m`)?  
5. **Skill install:** project-only (`.cursor/skills/blumkin`) vs also document personal `~/.cursor/skills/` for Copilot/Cursor across all repos?  
6. **MCP later:** skip until a concrete host requires it, or stub a no-op adapter early?  

---

## 12. References

- Private Graph lab (hand automations; not in this repo)  
- Identity follow-up: private Graph lab notes (Remedy WO — not published here)  
- Org tooling: [repository-helpers](https://github.com/the-hcma/repository-helpers) (`github-repo-lint`, `pre-pr-checks`, `start-development`)