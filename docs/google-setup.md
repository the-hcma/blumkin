# Google Workspace setup for blumkin

End-to-end guide to create the Google Cloud pieces Blumkin needs, wire a
local config profile, and smoke-test calendar/mail reads. Keep secrets out of
git, chat, and environment variables.

Prefer a **named profile** (for example `personal`) in the same
`~/.config/blumkin/config.toml` as Microsoft work. Legacy separate directories via
`BLUMKIN_CONFIG_DIR` (for example `~/.config/blumkin-google/`) still work.

---

## What Blumkin expects

| Piece | Where it lives | Notes |
|-------|----------------|--------|
| Desktop OAuth client JSON | Path of your choosing (mode `0600`, outside any repo) | Cloud Console download; includes `client_id` **and** `client_secret` |
| Config | `[profiles.<name>]` in `~/.config/blumkin/config.toml` | `provider = "google"` + `google_oauth_client_file = "…"` |
| User token | `~/.config/blumkin/profiles/<name>/google_token.json` | Written by `blumkin auth login` |

Credentials and settings come from **config.toml** and the Desktop client JSON.
Do **not** put `client_id` / `client_secret` / tenant-style overrides in env vars.
`BLUMKIN_CONFIG_DIR` only selects which config directory to use.
`BLUMKIN_PROFILE` / `--profile` select the profile **name or tag** (non-secret).

MVP verbs with `provider = "google"`: auth, calendar `today` / `view` /
`freebusy` / `suggest`, mail `inbox` / `list` / `get`. Other skills fail closed
until a later milestone ([#89](https://github.com/the-hcma/blumkin/issues/89)).

Requested OAuth scopes at login:

- `https://www.googleapis.com/auth/calendar.readonly`
- `https://www.googleapis.com/auth/calendar.freebusy`
- `https://www.googleapis.com/auth/gmail.readonly`

---

## A. Google Cloud Console (once per GCP project)

### 1. Project

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Top bar → project picker → **New project** (or select an existing one).
3. Open that project for the rest of these steps.

### 2. Enable APIs

**APIs & Services → Library**, enable both:

- **Google Calendar API**
- **Gmail API**

Without these, login may succeed but calendar/mail calls fail.

### 3. OAuth consent screen (Google Auth platform)

**APIs & Services → OAuth consent screen** (or **Google Auth platform** branding /
audience / data access, depending on Console UI):

1. **User type**
   - Prefer **Internal** when you are on Google Workspace **and** that option
     appears (org users only; no public verification for personal use).
   - Otherwise **External**.
2. App name / support email: anything sensible (for example `blumkin`).
3. **Scopes** (optional in the Console UI): Blumkin requests the three scopes
   above at login. If the UI asks you to register scopes, add those three.
4. **External + Testing (most personal setups):** under **Audience** / **Test
   users**, add the **exact** Google account you will use in the browser Allow
   flow. Apps in **Testing** reject sign-in for accounts that are not listed.
5. You do **not** need to **Publish** to production for personal blumkin. Stay in
   **Testing** unless you intentionally want a production app (verification,
   branding, and policy review then apply).

**Notes for Testing / External:**

- Unverified-app warnings in the browser are normal for a personal Desktop
  client. Choose your account → continue past the warning when you trust the
  app you created.
- Google may expire **refresh tokens** for apps left in Testing on a short
  cadence (commonly about seven days). Re-run `blumkin auth login` when silent
  refresh stops working, or move to Internal / production if your org allows it.

### 4. Create a Desktop OAuth client

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Desktop app**.
3. Name it (for example `blumkin-desktop`) → **Create**.
4. **Download JSON** (Console labels this like “Download JSON” / shows a
   `client_secret_….apps.googleusercontent.com.json` filename).

That file contains an `installed` (sometimes `web`) object with `client_id` and
`client_secret`. Desktop clients still ship a secret; Google’s token endpoint
rejects the exchange when it is missing. **Treat the whole JSON as secret.**

### 5. Store the client JSON safely

Do not leave it in `~/Downloads` under the Console’s long name.

```bash
mkdir -p ~/work/…/personal-automation   # or any private dir outside git
mv ~/Downloads/client_secret_*.apps.googleusercontent.com.json \
  ~/work/…/personal-automation/google-oauth-desktop-client.json
chmod 600 ~/work/…/personal-automation/google-oauth-desktop-client.json
```

Never commit this file. Never paste its contents into chat, issues, or PRs.

---

## B. Blumkin config profile

Add a Google profile alongside Microsoft in `~/.config/blumkin/config.toml`
(mode `0600`):

```toml
default_profile = "work"

[profiles.work]
provider = "microsoft"
client_id = "<entra-public-client-id>"
tenant_id = "<your-entra-tenant>"
default_tz = "America/New_York"
tags = ["@work", "work", "microsoft", "m365"]

[profiles.personal]
provider = "google"
default_tz = "America/New_York"
google_oauth_client_file = "/absolute/or/~/path/to/google-oauth-desktop-client.json"
tags = ["@personal", "personal", "google", "gmail"]
```

- Path only — **no** `client_secret` in toml.
- `client_id` is optional in the Google profile; when omitted, Blumkin reads it
  from the JSON.
- Token files land under `~/.config/blumkin/profiles/personal/`.
- Inspect keys without dumping secrets:

```bash
rg -n '^[a-z_]+|\[|tags' ~/.config/blumkin/config.toml \
  | sed -E 's/(client_id|client_secret|google_oauth_client_file|.*token.*)\s*=.*/\1 = "(redacted)"/I'
```

**Legacy:** a separate directory (`export BLUMKIN_CONFIG_DIR=~/.config/blumkin-google`
with a flat `config.toml`) still works if you have not migrated yet.

---

## C. Install / point the CLI

From a clone (or the PR worktree while developing):

```bash
uv sync --group dev
uv tool install -e .
blumkin --version
blumkin profiles list --json
```

Ensure the tool bin dir (often `~/.local/bin`) is on `PATH`. Agents and humans
invoke `blumkin …`, not `uv run blumkin`.

---

## D. Login and smoke (reads only)

The following commands assume the **named-profile** layout (for example
`[profiles.personal]`). For a legacy flat config under `BLUMKIN_CONFIG_DIR`,
omit `--profile` (implicit profile name `default`).

Always select the Google profile (name or tag) when using named profiles:

```bash
blumkin --profile personal auth login
# or: blumkin --profile @personal …
# or: export BLUMKIN_PROFILE=personal
# Legacy flat config: blumkin auth login
```

Interactive login needs a real TTY and a browser (do this in Terminal.app, not a
noninteractive agent shell).

Sign in as the **test user** (or Internal org user) you configured. Allow the
Calendar / Gmail scopes. On success,
`~/.config/blumkin/profiles/personal/google_token.json` appears (mode `0600`)
(legacy flat: `google_token.json` in the config dir root).

Check status (safe keys only):

```bash
blumkin --profile personal auth status --json
# Legacy flat: blumkin auth status --json
# expect provider=google, token/refresh present, access_token_expired false
```

Read smokes:

```bash
BLUMKIN_NONINTERACTIVE=1 blumkin --profile personal calendar today --json
BLUMKIN_NONINTERACTIVE=1 blumkin --profile personal mail inbox --top 5 --json
# Legacy flat: omit --profile on the same verbs
```

Agent / CI shells should set `BLUMKIN_NONINTERACTIVE=1` so Blumkin never opens a
browser; they rely on a prior interactive login on that machine.

Logout (deletes the Google token file for this profile only):

```bash
blumkin --profile personal auth logout
```

---

## E. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Access blocked / app not verified / only test users | External app in **Testing**; account not listed | Add the exact sign-in account under Test users; retry login |
| `(invalid_request) client_secret is missing` | Auth not reading Desktop JSON (or empty secret) | Set `google_oauth_client_file` to the Console download; confirm JSON has `installed.client_secret` |
| `auth_required` / exit 3 on calendar/mail | No token yet, or refresh expired | Run `blumkin --profile personal auth login` on a TTY |
| Calendar/mail HTTP 403 after login | API not enabled, or wrong GCP project | Enable Calendar + Gmail APIs on the **same** project as the OAuth client |
| Wrong mailbox / calendar | Wrong profile selected | Pass `--profile personal` / `@personal`, or check `blumkin profiles list --json` |
| Silent refresh dies after ~a week | Testing-mode refresh token policy | Re-login; consider Internal or production if appropriate |
| Agent opens a browser / hangs | Interactive auth in a non-TTY | Set `BLUMKIN_NONINTERACTIVE=1`; login once yourself first |

---

## F. Security checklist

- [ ] Desktop client JSON mode `0600`, outside any git repo
- [ ] `config.toml` mode `0600`; directory mode `700`
- [ ] No `client_secret` in toml, env, chat, or commits
- [ ] Named profiles (or legacy separate dirs) when Microsoft and Google coexist
- [ ] Never commit `google_token.json`, MSAL caches, or `.env`
- [ ] Prefer allowlisted status keys over dumping config/token files

---

## Related

- Parent epic: [#67](https://github.com/the-hcma/blumkin/issues/67)
- Post-MVP Google surface: [#89](https://github.com/the-hcma/blumkin/issues/89)
- Multi-provider agent context protocol: [#91](https://github.com/the-hcma/blumkin/issues/91)
- Agent skill notes: [`.cursor/skills/blumkin/SKILL.md`](../.cursor/skills/blumkin/SKILL.md)
- Agent integration overview: [`agent-integration.md`](./agent-integration.md)
