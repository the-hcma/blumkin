# Personal Microsoft Account (MSA) setup for blumkin

End-to-end guide for using blumkin with a **personal Microsoft account** -
`@live.com` / `@outlook.com` / `@hotmail.com` / `@msn.com`, or a personal
account on a custom domain that signs in through **consumers** rather than a
work/school Entra tenant. The README / `SECURITY-AT-A-GLANCE.md` notes on
`account_type = "personal"` are accurate but assume you already have an Entra
app registration; this guide starts from a bare MSA that has never touched
Azure/Entra before (issues #297, #362, #369).

This is a **documentation-only** guide - it does not change how blumkin
behaves, only how you set up the Entra side for a personal account.

---

## Is this you?

| Kind | Examples | `account_type` |
|------|----------|----------------|
| Classic Microsoft consumer domains | `@live.com`, `@outlook.com`, `@hotmail.com`, `@msn.com` | `"personal"` |
| Consumer account on a custom domain | A personal Microsoft account that signs in through **consumers** / MSA, not a work/school Entra tenant UPN | `"personal"` |
| Work/school account | `@yourcompany.com` synced to an org Entra tenant, M365 Business/Enterprise, etc. | `"organizational"` (default) - see the main [README](../README.md) instead |

blumkin never infers `account_type` from the email address or `tenant_id` -
you set both explicitly in `config.toml` (§C below). If you are not sure which
kind of account you have, look at how you sign in today: a work/school
account signs in at your company's branded login page or with an "Enterprise
sign-in" prompt; an MSA does not.

**Chat, meetings, and people-directory skills never work on a personal
account** - Entra will not grant `Chat.*`, `OnlineMeetings.ReadWrite`, or
`People.Read` to an MSA, so blumkin drops them from the requested scope set
for a `"personal"` profile and those skills fail closed with a usage error.
Calendar, mail, and mail auto-reply/signature are unaffected.

---

## A. Get an Entra directory (once)

You cannot register an app "outside a directory" anymore - the Azure portal
now shows *"The ability to create applications outside of a directory has
been deprecated"* for a bare MSA with no directory. You need a real Entra
directory first, then register the app **inside** it.

1. Open a **Private/Incognito window** (a normal window can already be signed
   into a stale session that lands you in the wrong tenant - see the
   troubleshooting table below).
2. Go to <https://azure.microsoft.com/free> and sign up for **Azure Free**
   using your MSA. This is free and does not require a paid subscription to
   finish - completing signup is what creates your own Entra directory (a
   `*.onmicrosoft.com` tenant), which is the part you actually need.
   - Alternative: the [Microsoft 365 Developer Program](https://developer.microsoft.com/microsoft-365/dev-program)
     also provisions a directory (an M365 E5 developer tenant), if you would
     rather have that instead of an Azure subscription - it requires meeting
     the program's own eligibility/qualification path (for example an active
     Microsoft 365 subscription in some tracks), so use Azure Free instead if
     you do not qualify.
   - Alternative: if you already administer a work/school Entra tenant, you
     can host the app registration there instead - but it must be a
     **separate** app from any work/school blumkin profile, with "Supported
     account types" allowing personal accounts (§B.1). Do not loosen an
     existing work/school app's account-type setting to accommodate this.
3. After signup, confirm you are in the *new* directory: open
   [Entra admin center](https://entra.microsoft.com), check the directory
   switcher (top right) for a `*.onmicrosoft.com` name you just created, and
   switch to it if it is not already selected.

**Azure CLI (`az login` / `az ad app`) is the wrong tool before this step** -
`az ad app` management needs a directory/subscription to operate against, and
`--tenant consumers` fails outright because Azure Resource Manager is
org-only. Use the portal for steps A and B; once your directory exists, `az`
can manage the app inside it if you prefer.

---

## B. Register the public client

In the **new** directory from step A (Entra admin center → **App
registrations** → **New registration**):

1. **Name**: anything sensible (for example `blumkin-personal`).
2. **Supported account types**: **Personal Microsoft accounts only** (or the
   multi-tenant + personal option, if you also want work/school accounts on
   the same app - most operators want personal-only here). The single-tenant
   "this organizational directory only" option that
   [`SECURITY-AT-A-GLANCE.md`](./SECURITY-AT-A-GLANCE.md#microsoft-app-registration-hardening)
   recommends for work/school profiles **will refuse an MSA sign-in
   outright** - do not use it for this app.
3. **Redirect URI**: leave blank for now; add it in the next step (a Web
   redirect added at creation time uses the wrong platform).
4. **Register**.
5. On the app's **Overview** page, copy the **Application (client) ID** - a
   GUID next to that exact label. This is what goes in `client_id` (§C). The
   **Object ID** on the same page is a different GUID for a different
   purpose; pasting it into `client_id` breaks login without an obvious error.
6. **Authentication** blade → **Add a platform** → **Mobile and desktop
   applications** (not Web, not Single-page application) → redirect URI
   `http://localhost` → **Configure**.
   - blumkin signs in with `InteractiveBrowserCredential`, which opens a
     system browser and listens on an ephemeral `http://localhost:<port>`.
     Only the **Mobile and desktop applications** platform's `http://localhost`
     entry matches that; a Web-platform redirect (even the identical-looking
     URL) does not, and blumkin's local listener will sit there indefinitely.
7. On the same **Authentication** blade, scroll to **Advanced settings** and
   set **Allow public client flows** to **Yes**. Leave everything else
   default.
8. **API permissions** (optional at this stage): you can add delegated Graph
   permissions here (`Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadWrite`,
   `User.Read`) if you want the list to be visible on the app, but a
   personal-account-only app registration **rejects several of these** via
   `az ad app permission add` / a Graph `PATCH` on the app
   (`InvalidRequiredResourceAccess`) even though the app itself is otherwise
   fine. This is a portal/API quirk, not a blocker: **dynamic user consent at
   `blumkin auth login` still grants them** regardless of what is listed
   here. Do not spend time fighting the portal to make this list complete -
   add what it will let you add, and let first login do the rest.

---

## C. Blumkin profile

Add a personal-account profile to `~/.config/blumkin/config.toml` (mode
`0600`), alongside any other profiles you already have:

```toml
default_profile = "work"   # unrelated existing profile, if any

[profiles.microsoft-personal]
provider = "microsoft"
account_type = "personal"
tenant_id = "consumers"
client_id = "<application-client-id-from-B.5>"
default_tz = "America/New_York"
email = "you@live.com"
tags = ["@msa", "microsoft-personal", "live", "msa", "outlook"]
```

- `account_type = "personal"` and `tenant_id = "consumers"` must **both** be
  set explicitly - blumkin never infers either from the other or from the
  email address. Prefer `"consumers"` over `"common"` unless you deliberately
  want this same app/profile to also accept work/school sign-ins.
- `email` is optional but recommended once you know it - it shows up in
  `blumkin profiles list --json` and helps an agent (or you) confirm which
  account a command is about to touch, especially once you have more than
  one profile.
- Pick a `tags` value that does not collide with an existing profile's tag
  (for example, do not reuse `@personal` if a Google profile already owns it
  - see [`docs/google-setup.md`](./google-setup.md)). With more than one
  profile configured, pass `--profile <name-or-tag>` explicitly on every
  command rather than relying on `default_profile`.
- No client secret anywhere - this is a public client (auth code + PKCE),
  same as every other blumkin Microsoft profile.

---

## D. First login

```bash
blumkin --profile microsoft-personal auth login
```

Interactive login needs a real TTY and a browser (Terminal.app, not a
non-interactive agent shell). Sign in as the MSA. Do **not** open
`http://localhost:<port>` yourself while the CLI is waiting, and do not run a
second `auth login` at the same time - both cause `state mismatch: … vs None`.

On success, `~/.config/blumkin/profiles/microsoft-personal/msal_token_cache.json`
and `auth_record.json` appear (mode `0600`).

Verify:

```bash
blumkin --profile microsoft-personal auth status --json
blumkin --profile microsoft-personal doctor --json
BLUMKIN_NONINTERACTIVE=1 blumkin --profile microsoft-personal calendar today --json
BLUMKIN_NONINTERACTIVE=1 blumkin --profile microsoft-personal mail inbox --top 5 --json
```

---

## E. Scopes and capabilities

Base effective scopes for a `"personal"` profile: `User.Read`,
`Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadWrite`. Optional add-ons still
apply if you turn them on and re-consent: `Files.ReadWrite` (`docs_scopes`),
`Files.Read` (`files_scopes`), `MailboxSettings.ReadWrite`
(`wo1162425_scopes`, which also brings the Teams/people scopes on an
organizational profile - on a personal profile blumkin still drops the
Teams/people portion of that flag, since personal accounts can never be
granted them).

Never available on a personal account, regardless of config: `Chat.Read`,
`Chat.ReadWrite`, `OnlineMeetings.ReadWrite`, `People.Read`. `chat.*`,
`meeting.*`, and `people.resolve` fail closed with a usage error explaining
why - this is by design, not a bug to work around.

---

## F. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Portal error: *"The ability to create applications outside of a directory has been deprecated"* | No Entra directory exists yet for this MSA | Complete Azure Free (or M365 Developer Program) signup first (§A) |
| Sign-in lands you in a "Microsoft Services" tenant; `AADSTS50020` "Selected user account does not exist in tenant 'Microsoft Services'" | A bare MSA with no directory defaults into a locked service tenant that cannot host app registrations | Complete Azure Free signup in a **Private window** (a stale session can otherwise keep landing you back in the wrong tenant); then use the **new** directory's App registrations blade |
| `az ad app` commands fail, or account list is empty | No directory/subscription exists yet, or `--tenant consumers` was passed | Finish §A first; use the portal for app registration until then |
| Login succeeds in the browser but the CLI keeps waiting on `:<port>` | Redirect URI registered under **Web**, not **Mobile and desktop applications** | Add `http://localhost` under the correct platform (§B.6); remove/ignore any Web redirect |
| `state mismatch: … vs None` | Opened `localhost` manually, or ran two logins at once | Close extra tabs, kill the hung `auth login`, retry once |
| Login works but calls fail as if signed in wrong | Pasted **Object ID** instead of **Application (client) ID** into `client_id` | Re-copy the client ID from the app's Overview page (§B.5) |
| `missing_scope` (exit 4) on mail/calendar right after first login | Portal API permissions list was incomplete for this personal-only app | Ignore the portal list; re-run `auth login` to re-consent - dynamic consent grants what the portal API blocked |
| `chat.*` / `meeting.*` / `people.resolve` fail with a usage error | Expected - personal accounts can never be granted these scopes | Not a bug; use a work/school profile for these skills |
| Azure shows the app registered and consented, but blumkin has no token | `auth login` did not complete, or wrote to a different profile/config dir | Re-run `blumkin --profile microsoft-personal auth login`; check `BLUMKIN_CONFIG_DIR` if set |

---

## G. Security pointers

- `tenant_id = "consumers"` reopens the wider device-code-phishing surface
  that [`SECURITY-AT-A-GLANCE.md`](./SECURITY-AT-A-GLANCE.md#microsoft-app-registration-hardening)
  otherwise narrows for single-tenant work/school apps - only set it on a
  profile you know signs into a personal account.
- Register a **separate** app for personal-account profiles; never loosen a
  work/school app's account-type setting instead.
- This remains a public client only (auth code + PKCE) - never add a client
  secret to this app registration or to `config.toml`.
- Tokens land under `~/.config/blumkin/profiles/microsoft-personal/` (mode
  `0600`); `token_storage` controls whether they also go to the OS keychain.
- Use only placeholder client IDs in issues, PRs, or chat - a `client_id` is
  not itself secret, but there is no reason to publish yours.

---

## Related

- Feature: `account_type = "personal"` ([#297](https://github.com/the-hcma/blumkin/issues/297),
  [#362](https://github.com/the-hcma/blumkin/issues/362))
- This guide: [#369](https://github.com/the-hcma/blumkin/issues/369)
- App-credential keychain / toml centralization
  ([#368](https://github.com/the-hcma/blumkin/issues/368)) may later change
  where `client_id` lives - update §C when that lands.
- [`README.md`](../README.md) - the general Microsoft/Google config reference
- [`docs/SECURITY-AT-A-GLANCE.md`](./SECURITY-AT-A-GLANCE.md#microsoft-app-registration-hardening) -
  app-registration hardening checklist
- [`docs/google-setup.md`](./google-setup.md) - the Google equivalent of this guide
- [`.agents/skills/blumkin/SKILL.md`](../.agents/skills/blumkin/SKILL.md) -
  agent cold-start notes
