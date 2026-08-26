# Handoff — Graph lab → Blumkin CLI

**Date:** 2026-08-26  
**Purpose:** Continue in a new session without re-deriving context.  
**Status:** M1 shipped ([#10](https://github.com/the-hcma/blumkin/pull/10)); M1 retro closed ([#11](https://github.com/the-hcma/blumkin/issues/11) / [`RETROSPECTIVE-M1.md`](./RETROSPECTIVE-M1.md)). Phases 2–3 read/write skills are on `main`. Prefer validating new Graph flows in the private lab, then porting them as Blumkin skills.

---

## What Blumkin is

- Public repo: [the-hcma/blumkin](https://github.com/the-hcma/blumkin).
- Goal: Python **`blumkin`** CLI on `PATH` — delegated Microsoft Graph “as me”, skill-shaped commands, `--json` for agents.
- Agent integration: **Cursor Agent Skill + shell** (`.cursor/skills/blumkin/SKILL.md`); Copilot CLI docs later — **not MCP** for v1 (see `PLAN.md` §6).
- Org practices via `repository-helpers` / `github-repo-lint` (`AGENTS.md`, LICENSE, `.cursor/rules`, `.github/stacking-tool` = `gh-stack`, CODEOWNERS).

**Read first in a new session:** `README.md` → `PLAN.md` → this file → `AGENTS.md`.

---

## Lab that already works (hand automations)

A private Graph lab (separate from this repo) already exercises delegated auth and the flows below. Keep client IDs and token caches **out of git**.

| Piece | Role |
|-------|------|
| Local client-id config (gitignored) | Entra public-client ID — **do not commit** |
| Graph client + config helpers | Auth + Graph client; file token cache + auth record required for silent login |
| Token cache + auth record (gitignored) | Silent auth |
| Probe scripts | Access / readonly explore / token status |
| Entra app | **BRK Tech Microsoft Agent** — client ID in local config only; tenant `brk.tech` |
| Mode | **Delegated** / interactive browser / `http://localhost` |

### Flows already exercised by hand (CLI skill candidates)

| Flow | Notes |
|------|--------|
| Calendar today | TZ bug fixed: Graph UTC → America/New_York |
| Free/busy | Proven against real calendars |
| Accept pending invites | Proven |
| Create / cancel meeting | Proven |
| Mail inbox | List recent |
| Mail draft + send | Draft then send |
| Teams chat read | Last messages in 1:1 chats |
| Teams chat send | **Blocked** — need `Chat.ReadWrite` |
| Online meeting transcription flags | **Blocked** — need `OnlineMeetings.ReadWrite` |
| Channel message bodies | **Blocked** — need `ChannelMessage.Read.All` |

Auth caching: Keychain-only failed under Cursor; **file cache + AuthenticationRecord** works. Re-auth forces browser if those files are deleted.

---

## Identity / permissions follow-up (open)

A Remedy follow-up is open for **delegated** add-ons (ticket details stay in the private Graph lab — not this repo).

**Working assumption:** those add-ons will be granted. Phase 4 CLI skills are **implemented with mocked tests**; do not treat live Graph success as proven until the validation TODO below passes.

**Already granted / in use:** `Calendars.ReadWrite`, `Chat.Read`, `Mail.ReadWrite`, `Mail.Send`, `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `User.Read`. Keep those; request add-ons (Blumkin auth `SCOPES` already lists `Chat.ReadWrite` + `OnlineMeetings.ReadWrite` for re-consent):

- Teams: `Chat.ReadWrite`, `ChannelMessage.Read.All`, `ChannelMessage.Send`, `OnlineMeetings.ReadWrite`, `OnlineMeetingTranscript.Read.All`, `Presence.Read`
- Mailbox: `MailboxSettings.Read`
- Productivity: `Files.ReadWrite`, `Tasks.ReadWrite`, `Contacts.ReadWrite`, `People.Read`, `Notes.ReadWrite`

- [ ] **TODO (validate live after grant):** add the **new** scopes to the Entra client, delete token cache + auth record under the effective config dir (`BLUMKIN_CONFIG_DIR`, else `$XDG_CONFIG_HOME/blumkin` if set, else `~/.config/blumkin/`), `blumkin auth login`, confirm consent includes at least `Chat.ReadWrite` and `OnlineMeetings.ReadWrite`, then smoke `chat send|edit|delete` + `meeting get|transcription` against Graph.

**Do not** request app-only permissions, broad shared permissions, or RSC all-messages.
Keep the delegated `*.All` scopes listed above only when the corresponding flow requires them.

---

## What to do next (ordered)

1. **Agent DX (Phase 5):** [#20](https://github.com/the-hcma/blumkin/issues/20) — personal skill install + Copilot CLI instruction snippet — shell to `blumkin`, not MCP first.
2. **Human:** run any **new hand automation** in the private lab. Note: command, Graph APIs, scopes, failure modes.
3. **After Identity grant (validate TODO above):** live-smoke Phase 4 skills (`chat send|edit|delete`, `meeting get|transcription`); check off the TODO.
4. **Bugbot validate TODO:** after Bugbot is enabled on this repo, confirm a real review on a PR head (`RETROSPECTIVE-M1.md`).

---

## Intentional non-work in this repo right now

- Do not block new hand automation on Blumkin implementation.
- Do not claim Phase 4 live Graph coverage until the Identity **validate live** TODO is checked off.

---

## Open plan questions (still for review)

See `PLAN.md` §11 — config path, people resolve, migrate lab, default meeting duration, skill install location, MCP-later.
