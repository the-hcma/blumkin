# Handoff — from personal Graph lab → Blumkin CLI

**Date:** 2026-08-25  
**Purpose:** Continue in a new session without re-deriving context.  
**Next step for the human:** try **another automation by hand** first (likely still in `~/work/brk-tech/personal-automation`). Port to Blumkin CLI **later** — do not block that experiment on implementing this repo’s code.

---

## What Blumkin is

- Local repo: `~/work/ai/blumkin` (git on `main`, **no commits required yet** unless asked).
- Goal: Python **`blumkin`** CLI — delegated Microsoft Graph “as me”, skill-shaped commands, `--json` for agents.
- Agent integration (planned): **Cursor Agent Skill + shell** and **Copilot CLI** instructions — **not MCP** for v1 (see `PLAN.md` §6).
- Org practices seeded for `repository-helpers` / `github-repo-lint` (`AGENTS.md`, LICENSE, `.cursor/rules`, `.github/stacking-tool` = `gh-stack`, CODEOWNERS).

**Read first in a new session:** `README.md` → `PLAN.md` → this file → `AGENTS.md`.

---

## Lab that already works (hand automations)

Path: `~/work/brk-tech/personal-automation`

| Piece | Role |
|-------|------|
| `automation-info` | Client ID (gitignored); **do not commit** |
| `graph_client.py` / `graph_config.py` | Auth + Graph client; file token cache + auth record required for silent login |
| `.msal_token_cache.json`, `.auth_record.json` | Silent auth (gitignored) |
| `check_graph_access.py`, `explore_readonly.py`, `token_status.py` | Probes |
| Entra app | **BRK Tech Microsoft Agent** — client ID in local `automation-info` only (never commit); tenant `brk.tech` |
| Mode | **Delegated** / interactive browser / `http://localhost` |

### Flows already exercised by hand (CLI skill candidates)

| Flow | Notes |
|------|--------|
| Calendar today | TZ bug fixed: Graph UTC → America/New_York |
| Free/busy | e.g. Vivek Thu 5pm free; Aug 31 noon tentative |
| Accept pending invites | Rachel + Vivek meetings accepted |
| Create / cancel meeting | Created then cancelled Vivek Wed 11am |
| Mail inbox | List recent |
| Mail draft + send | Draft then send to `hcma@hcma.info` |
| Teams chat read | Last messages (e.g. Daniel Erickson, David McKenzie) |
| Teams chat send | **Blocked** — need `Chat.ReadWrite` |
| Online meeting transcription flags | **Blocked** — need `OnlineMeetings.ReadWrite` |
| Channel message bodies | **Blocked** — need `ChannelMessage.Read.All` |

Auth caching: Keychain-only failed under Cursor; **file cache + AuthenticationRecord** works. Re-auth forces browser if those files are deleted.

---

## Identity / permissions follow-up (open)

Copy/paste for Remedy helper:  
`~/work/brk-tech/personal-automation/tmp-followup-WO0000001161564.txt`  

References existing WO **`WO0000001161564`**. Asks for **delegated** add-ons (keep existing scopes):

- Teams: `Chat.ReadWrite`, `ChannelMessage.Read.All`, `ChannelMessage.Send`, `OnlineMeetings.ReadWrite`, `OnlineMeetingTranscript.Read.All`, `Presence.Read`
- Mailbox: `MailboxSettings.Read`
- Productivity: `Files.ReadWrite`, `Tasks.ReadWrite`, `Contacts.ReadWrite`, `People.Read`, `Notes.ReadWrite`

Full ticket: `TICKET-graph-access-followup.md`  
After grant: add scopes to client, delete token cache + auth record, re-login.

**Do not** request app-only / broad shared `*.All` / RSC all-messages in that WO.

---

## What to do next (ordered)

1. **Human:** run the **new hand automation** you care about in `personal-automation` (or a scratch script there). Note: command, Graph APIs, scopes, failure modes.
2. **When ready for CLI:** implement per `PLAN.md` phases (Phase 1 skeleton → read skills → write skills). Add a row to the skill inventory for the new hand flow.
3. **When Identity returns:** wire follow-up scopes; unlock chat write + meeting transcription skills.
4. **GitHub / lint:** after remote exists, `github-repo-lint --new-repo/--suggest/--apply-fix` (see `PLAN.md` §8).
5. **Agent DX (Phase 5):** `.cursor/skills/blumkin/SKILL.md` + Copilot CLI instruction snippet — shell to `blumkin`, not MCP first.

---

## Intentional non-work in this repo right now

- No application code yet (by design until hand experiments settle).
- No initial git commit unless requested.
- Do not block new hand automation on Blumkin implementation.

---

## Open plan questions (still for review)

See `PLAN.md` §11 — config path, people resolve, migrate lab, GitHub slug, default meeting duration, skill install location, MCP-later.

---

## Quick commands (lab, today)

```bash
cd ~/work/brk-tech/personal-automation
source .venv/bin/activate
python token_status.py
python -c "..."   # ad-hoc Graph; prefer extending scripts carefully
```

```bash
cd ~/work/ai/blumkin
# read PLAN.md / HANDOFF.md — implement later
```
