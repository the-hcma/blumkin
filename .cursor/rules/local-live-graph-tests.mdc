---
description: Run live Graph/auth tests locally (CI mocks only)
globs: src/blumkin/auth.py,src/blumkin/graph.py,src/blumkin/skills/**/*.py,tests/test_auth*.py,tests/test_live*.py
alwaysApply: false
---

# Local live Graph / auth tests

CI runs **mocked** unit tests only (`pytest -m 'not live'`). Silent refresh and real Graph reads are validated **on this machine**.

Live means **real**: never verify with a skill that notifies others (no invites,
sends, or chats). See `.cursor/rules/no-third-party-side-effects.mdc`.

When changing auth, Graph client, calendar/mail/chat skills, or live/auth tests:

1. Unit (same as CI):
   ```bash
   uv run pytest -m 'not live'
   ```
2. Live reads + forced access-token expiry → silent refresh:
   ```bash
   BLUMKIN_LIVE=1 uv run pytest -m live
   ```
   Requires config + token cache + auth record with a refresh token under
   `~/.config/blumkin/` (or `BLUMKIN_CONFIG_DIR`). Do not commit those files.

3. Smoke CLI after live green:
   ```bash
   blumkin auth status
   blumkin calendar today --json
   ```

Do not skip step 2 for auth/token-cache changes: the live test force-expires the cached access token and asserts Graph still works and `auth status` shows a future expiry.

## Google provider (`provider = "google"`)

`tests/test_live_google_reads.py` is the Google equivalent. It is marked both
`live` and `live_google`, so CI's `-m 'not live'` still deselects it; run it
against a logged-in Google profile:

```bash
BLUMKIN_LIVE_GOOGLE=1 uv run pytest -m live_google
```

Requires a Google profile (`provider = "google"`, Desktop client JSON, token
file with a refresh token) under `~/.config/blumkin/` (or `BLUMKIN_CONFIG_DIR`
/ `BLUMKIN_PROFILE`). Reads only — never a notifying verb.
