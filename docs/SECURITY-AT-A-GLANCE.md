# Security at a glance

One page. Full policy: [`SECURITY.md`](../SECURITY.md).

## What blumkin is

A local CLI that acts **as the signed-in user** against their own Microsoft 365
or Google Workspace, over delegated OAuth. No server, no bot, no app-only
permissions, no multi-tenant anything.

## What it touches, and where that lives

| Data | Location | In git? |
|------|----------|---------|
| OAuth client id (public client) | `~/.config/blumkin/config.toml` (mode `0600`) - or vault it in the OS keychain instead (`blumkin auth setup` or `blumkin auth set-app-secret --kind ms_client_id`, issue #368), which then takes precedence | never |
| Token cache + auth record / Google token | `~/.config/blumkin/profiles/<name>/` (file backend), or the OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service) when `token_storage` selects it - see below | never |
| Optional in-memory re-verify cache | macOS `blumkin-agent` process only, for up to `token_reverify_after` per profile (default `24h`); an expired entry is lazily wiped on the next access, not on an active timer | never |
| Google desktop-client JSON (holds `client_secret`) | operator-chosen path, mode `0600` - or vault `client_secret` in the OS keychain instead (`blumkin auth setup` or `blumkin auth set-app-secret --kind google_client_secret`, issue #368), which then takes precedence and makes the file optional | never |
| The user's mail / calendar / chat content | fetched on demand, printed to stdout, not persisted | n/a |

[`.gitignore`](../.gitignore), GitHub **secret-scanning push protection** (blocks
a recognised secret before it is pushed), and the `gitleaks` CI check
([`.github/ci/secret-scan`](../.github/ci/secret-scan), advisory - runs every PR)
keep all of the above out of the repo. blumkin never writes another person's
data anywhere.

## Auth model

- **Delegated only.** Interactive browser sign-in (public client + `localhost`
  redirect). No client secret for Microsoft flows; the Google secret stays in
  the desktop-client JSON, never in toml or env.
- The token cache and auth record are written under `~/.config/blumkin/` by
  default (the plain `0600` file backend, one file each) unless a real
  keychain backend is usable at runtime, in which case the two share **one**
  keychain item instead of one each - every real sign-in flow reads or
  writes both together, so a separate item per secret would mean a separate
  OS Keychain authorization prompt for each on what is, from the operator's
  point of view, a single sign-in. `keyring` (the `keychain` extra) is a core
  dependency on macOS (`pipx install blumkin` always pulls it in there) and
  remains an opt-in extra elsewhere (`pipx install 'blumkin[keychain]'`).
  `token_storage = "auto"` (the default in
  `config.toml`) prefers the OS keychain (macOS Keychain, Windows Credential
  Manager, Linux Secret Service) whenever that is the case. Only a *write*
  silently falls back to the plain file for most synchronous runtime
  trouble (headless Linux with no Secret Service, `keyring` not installed,
  a keychain write failing outright, etc.) - a *delete* (`auth logout`) has
  no file fallback and instead raises on any keychain failure, since
  silently reporting a successful logout while credentials remain in the
  keychain would be worse than a loud, actionable error. It raises rather
  than falling back or reporting success when a keychain *write or delete*
  call *times out* (a hung/locked backend), since the abandoned call keeps
  running and could still complete later - after a newer login/logout for
  the same profile - and silently clobber or resurrect state, so the
  caller is told to retry rather than risk that; a *read* that times out is
  simply treated as "nothing found there yet" and falls through to the
  file, since there is nothing to lose by retrying a read later. It also
  raises if a fallback write's stale-keyring-entry cleanup itself cannot be
  reconciled, which would otherwise leave the two backends silently
  disagreeing. `token_storage = "keyring"` pins the keychain: it warns once
  (not on every call) when no usable backend is found, and raises if a
  write or delete to the keychain fails for any reason (timeout included),
  since the operator explicitly asked for it and a silent downgrade to the
  file would defeat that choice.
  `token_storage = "file"` forces the file unconditionally, even when a
  keychain backend is available. Neither backend does cryptographic
  or host binding - a copied file, or a keychain item exported off the
  machine, will refresh on another host with the client id / tenant. Protect
  the directory (and, for the keychain path, the OS account); revoke
  tenant-side (or remove the app grant) if it leaks. See issue #287.
- Silent refresh renews access tokens without a browser; deleting the cache
  (`blumkin auth logout`) forces a fresh sign-in.
- On macOS, a profile whose `token_reverify_after` is not `0`/`"never"`
  may also use `blumkin-agent` as a short-lived, in-memory cache in front
  of the real backend. The agent stores decrypted credentials only in RAM,
  behind a local presence check, and wipes an entry once its TTL has
  elapsed - lazily, on the next `get`/status check/`blumkin agent lock`/
  process exit, rather than via an active expiry sweep the instant the TTL
  ends. If the agent is unavailable, blumkin falls back to the direct
  backend path above.

## Microsoft app registration hardening

A public client's `client_id` is not itself a secret - it is routinely visible
in redirect URLs and can be embedded in an open-source client without
weakening security by itself. It is not nothing, though: anyone who learns it
can attempt their own OAuth flow against it, most notably **device-code-flow
phishing** (a real technique, e.g. used by Nobelium/APT29 against unrelated
victims via legitimate public client ids) - the attacker starts a device-code
request using the known `client_id`, then tricks a real user into completing
it on Microsoft's genuine login page; the resulting token goes to the
attacker, not to blumkin. blumkin's requested scopes are sensitive enough
that this is worth configuring against, not just accepting: `BASE_SCOPES`
(always requested for an `account_type = "organizational"` profile) covers
`Calendars.ReadWrite`, `Chat.Read`, `Mail.ReadWrite`, `Mail.Send`, `User.Read`;
opt-in config flags can add `Files.ReadWrite` (`docs_scopes`), `Files.Read`
(`files_scopes`), or `Chat.ReadWrite` / `MailboxSettings.ReadWrite` /
`OnlineMeetings.ReadWrite` / `People.Read` (`wo1162425_scopes` -
`MailboxSettings.ReadWrite` in particular grants control over mailbox
auto-forward/auto-reply rules, so treat it as the most sensitive of the set).
A profile with `account_type = "personal"` never gets `Chat.Read`,
`Chat.ReadWrite`, `OnlineMeetings.ReadWrite`, or `People.Read` - personal
Microsoft accounts cannot be granted them, so blumkin drops them from the
requested set instead of requesting and failing. See `src/blumkin/auth.py`
for the authoritative, current lists. blumkin itself never uses device-code or ROPC flows -
only interactive browser sign-in (auth code + PKCE, `localhost` redirect) -
so the mitigations below narrow this registration's exposure without
changing how blumkin signs in:

- **Single-tenant, not multi-tenant.** Set "Supported account types" to
  accounts in *this organizational directory only*, and set `tenant_id` in
  `config.toml` to your tenant's specific GUID or verified domain - never
  `common` / `organizations` / `consumers`. This is a per-installation
  setting: each operator registers their own app in their own tenant, so
  restricting yours has no effect on anyone else's ability to run blumkin.
  It bounds who can even attempt to sign in to *this* registration to actual
  members of *your* tenant, rather than anyone on the internet.
  A profile with `account_type = "personal"` (issue #297) is the one
  deliberate exception: personal Microsoft accounts have no dedicated tenant
  GUID, so that profile needs `tenant_id = "consumers"` (or `"common"`) and
  reopens the wider device-code-phishing surface this bullet otherwise
  narrows. Only set `account_type = "personal"` / a non-tenant-scoped
  `tenant_id` on a profile you know signs into a personal account - never as
  a default or a work/school tenant's fallback. Such a profile also needs its
  **own, separate** Entra app registration with "Supported account types" set
  to allow personal Microsoft accounts - a registration whose "Supported
  account types" is scoped to *this organizational directory only* (as this
  bullet recommends for work/school profiles) will refuse a personal-account
  sign-in outright, regardless of `account_type` / `tenant_id`. Do not loosen
  a work/school registration's account-type setting to accommodate a
  personal-account profile; register a second app instead. For a full
  walkthrough of registering that second app and configuring the profile
  from scratch, see
  [`docs/microsoft-personal-setup.md`](./microsoft-personal-setup.md).
- **Leave "Allow public client flows" enabled, but rely on the other
  mitigations here instead of disabling it.** `InteractiveBrowserCredential`'s
  auth-code-plus-PKCE flow is *itself* a public client flow and needs this
  setting on - blumkin's own sign-in breaks without it. It does not
  distinguish auth-code-plus-PKCE from device code / ROPC, so it cannot be
  used to allow one and block the other; single-tenant scope, redirect URI
  restriction, and assignment requirement are what actually narrow the
  device-code-phishing surface here.
- **Restrict redirect URIs** to `http://localhost` (loopback) registered
  under the **Mobile and desktop applications** platform (not Web or SPA -
  SPA redirect URIs can't be used with this non-SPA flow and will break
  sign-in), with no wildcards - this is what `InteractiveBrowserCredential`
  uses and all it needs.
- **Consider "assignment required"** on the corresponding Enterprise
  Application if your tenant has more than one member, so only explicitly
  assigned users/groups can even complete sign-in, further narrowing who a
  phishing attempt could target.

None of this is enforced by blumkin's code - it is Entra-side configuration
on the app registration itself, done once at setup.

## Blast radius

- A skill only ever affects the operator's own tenant, with their own consent.
- Actions that reach other people (mail send, calendar invite, chat) require an
  explicit `--yes` and a verb that says so - read-looking commands never send.
- Worst case for a stolen `~/.config/blumkin/`: the attacker can act as the
  operator until the refresh token is revoked (tenant-side) or the app grant is
  removed.

## How releases are trusted

1. Every PR: agent review + code-owner review, plus the required status checks
   in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) all green -
   `Scaffold checks`, `Python lint & format checks`
   (`ruff` / `pyright`, [`.github/ci/python-static`](../.github/ci/python-static)),
   `Pytest (hermetic)` ([`.github/ci/pytest-hermetic`](../.github/ci/pytest-hermetic)),
   `Packaging smoke` ([`test_packaging`](../test_packaging)), and `Shellcheck`
   ([`.github/ci/shellcheck`](../.github/ci/shellcheck)).
2. Release cut by Release Please from Conventional Commits
   ([`.github/workflows/release-please.yml`](../.github/workflows/release-please.yml),
   [`release-please-config.json`](../release-please-config.json)) - no
   hand-edited versions.
3. The `publish-pypi` job runs in the `pypi` environment, which **pauses for
   @thehcma to approve** and only deploys from `main` / `blumkin-v*`. Its
   actions are SHA-pinned; the workflow token is read-only.
4. Published to PyPI by **OIDC trusted publishing** (no stored token). The wheel
   carries the release commit via
   [`scripts/embed_build_metadata`](../scripts/embed_build_metadata) /
   [`src/blumkin/version.py`](../src/blumkin/version.py) - `blumkin --version`
   shows it.
5. [`scripts/verify-pypi-release`](../scripts/verify-pypi-release) installs the
   published artifact in isolation, and again via a real `pipx install`, and
   checks the version and commit both ways before the job is green.

Compliance of the branch protection, workflows, and cursor rules that back all
of this is enforced by `github-repo-lint` from
[`the-hcma/repository-helpers`](https://github.com/the-hcma/repository-helpers) -
see [`SECURITY.md` › Governance tooling](../SECURITY.md#governance-tooling-repository-helpers).

## Vulnerabilities

- Dependencies: `pip-audit` daily
  ([`.github/workflows/cve-check.yml`](../.github/workflows/cve-check.yml)) +
  Dependabot ([`.github/dependabot.yml`](../.github/dependabot.yml), 10-day
  cooldown).
- Report a vulnerability privately:
  <https://github.com/the-hcma/blumkin/security/advisories/new>. Targets and
  scope are in [`SECURITY.md`](../SECURITY.md).
