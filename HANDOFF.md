# Handoff — Identity grant status (WO0000001162425)

**Last updated:** 2026-09-12.
**Purpose:** Track the one open cross-cutting item this repo can't close itself —
the delegated-scope grant follow-up — so `auth.py` / `cli.py` / `dispatch.py` /
the Cursor skill have one place to point to. For everything else (what's
shipped, current command surface, design rationale), read `README.md`,
`CHANGELOG.md`, and `docs/DECISIONS.md` — they track current state; this file
does not.

---

## Identity / permissions follow-up (open)

Remedy **WO0000001162425** is the follow-up for **delegated** scope add-ons
beyond the original Entra app grant. **State (2026-08-28, last checked
2026-09-12):** the augmented asks below are on the ticket but **not fully
granted yet** (consent still hits admin approval for scopes such as
`People.Read`). Do not enable `wo1162425_scopes` / expect live `people
resolve` until Identity finishes the grant — `_wo1162425_scopes_enabled()` in
`config.py` still defaults to off, which is the current source of truth for
"not granted yet."

**Already granted / in use:** `Calendars.ReadWrite`, `Chat.Read`, `Mail.ReadWrite`,
`Mail.Send`, `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `User.Read`. Keep those.

**Augmented WO1162425 ask (pending fulfillment):**

- Teams: `Chat.ReadWrite`, `ChannelMessage.Read.All`, `ChannelMessage.Send`,
  `OnlineMeetings.ReadWrite`, `OnlineMeetingTranscript.Read.All`, `Presence.Read`
- Mailbox: `MailboxSettings.Read`
- Productivity: `Files.ReadWrite`, `Tasks.ReadWrite`, `Contacts.ReadWrite`,
  `People.Read`, `Notes.ReadWrite`

**Runtime gating today:** `wo1162425_scopes` requests only the scopes Blumkin
actually uses (`Chat.ReadWrite`, `OnlineMeetings.ReadWrite`, `People.Read`).
`files_scopes` is a separate opt-in for chat attachment download
(`Files.Read`). `docs_scopes` is a third opt-in: `Files.ReadWrite` for `docs
create` (uploads a `.docx` to OneDrive). All three need the Entra grant +
re-consent before they work live.

- [ ] **TODO (validate live after grant):** add the **new** scopes to the Entra
  client, delete token cache + auth record under the effective config dir
  (`BLUMKIN_CONFIG_DIR`, else `$XDG_CONFIG_HOME/blumkin` if set, else
  `~/.config/blumkin/`), set `wo1162425_scopes = true`, `blumkin auth login`,
  confirm consent includes at least `Chat.ReadWrite`, `OnlineMeetings.ReadWrite`,
  and `People.Read`, then smoke `chat send|edit|delete`, `meeting get|transcription`,
  and `people resolve` against Graph.

**Do not** request app-only permissions, broad shared permissions, or RSC all-messages.
Keep the delegated `*.All` scopes listed above only when the corresponding flow requires them.

---

## Other open TODOs tracked elsewhere (not duplicated here)

- Bugbot real-review validation once enabled on this repo — `RETROSPECTIVE-M1.md`.
- `calendar update --no-teams` live validation against Microsoft Graph (Google
  side is already live-tested) — `docs/DECISIONS.md` D9.
