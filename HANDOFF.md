# Handoff — Graph lab → Blumkin CLI

**Date:** 2026-08-25  
**Purpose:** Continue in a new session without re-deriving context.  
**Status:** M1 CLI skeleton shipped ([#10](https://github.com/the-hcma/blumkin/pull/10)). Prefer validating new Graph flows in the private lab, then porting them as Blumkin skills.

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

A Remedy follow-up is open for **delegated** add-ons (ticket details stay in the private Graph lab — not this repo). **Already granted / in use:** `Calendars.ReadWrite`, `Chat.Read`, `Mail.ReadWrite`, `Mail.Send`, `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `User.Read`. Keep those; request add-ons:

- Teams: `Chat.ReadWrite`, `ChannelMessage.Read.All`, `ChannelMessage.Send`, `OnlineMeetings.ReadWrite`, `OnlineMeetingTranscript.Read.All`, `Presence.Read`
- Mailbox: `MailboxSettings.Read`
- Productivity: `Files.ReadWrite`, `Tasks.ReadWrite`, `Contacts.ReadWrite`, `People.Read`, `Notes.ReadWrite`

After grant: add the **new** scopes to the client, delete token cache + auth record, re-login.

**Do not** request app-only / broad shared `*.All` / RSC all-messages in that follow-up.

---

## What to do next (ordered)

1. **Human:** run the **new hand automation** you care about in the private lab (or a scratch script there). Note: command, Graph APIs, scopes, failure modes.
2. **When ready for CLI:** port proven flows per `PLAN.md` Phase 2+ (read skills → write skills). Add a row to the skill inventory for the new hand flow.
3. **When Identity returns:** wire follow-up scopes; unlock chat write + meeting transcription skills.
4. **Agent DX (Phase 5):** personal skill install + Copilot CLI instruction snippet — shell to `blumkin`, not MCP first.

---

## Intentional non-work in this repo right now

- Do not block new hand automation on Blumkin implementation.
- Identity-blocked scopes stay out until the private-lab follow-up lands.

---

## Open plan questions (still for review)

See `PLAN.md` §11 — config path, people resolve, migrate lab, default meeting duration, skill install location, MCP-later.
