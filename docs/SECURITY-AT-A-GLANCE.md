# Security at a glance

One page. Full policy: [`SECURITY.md`](../SECURITY.md).

## What blumkin is

A local CLI that acts **as the signed-in user** against their own Microsoft 365
or Google Workspace, over delegated OAuth. No server, no bot, no app-only
permissions, no multi-tenant anything.

## What it touches, and where that lives

| Data | Location | In git? |
|------|----------|---------|
| OAuth client id (public client) | `~/.config/blumkin/config.toml` (mode `0600`) | never |
| Token cache + auth record / Google token | `~/.config/blumkin/profiles/<name>/` (file backend), or the OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service) when `token_storage` selects it - see below | never |
| Google desktop-client JSON (holds `client_secret`) | operator-chosen path, mode `0600` | never |
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
  default (the plain `0600` file backend) unless the optional `keychain`
  extra (`pipx install 'blumkin[keychain]'`) is installed and a real backend
  is usable at runtime. `token_storage = "auto"` (the default in
  `config.toml`) prefers the OS keychain (macOS Keychain, Windows Credential
  Manager, Linux Secret Service) whenever that is the case. Only a *write*
  silently falls back to the plain file for most synchronous runtime
  trouble (headless Linux with no Secret Service, the extra not installed,
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

## Microsoft app registration hardening

A public client's `client_id` is not itself a secret - it is routinely visible
in redirect URLs and can be embedded in an open-source client without
weakening security by itself. It is not nothing, though: anyone who learns it
can attempt their own OAuth flow against it, most notably **device-code-flow
phishing** (a real technique, e.g. used by Nobelium/APT29 against unrelated
victims via legitimate public client ids) - the attacker starts a device-code
request using the known `client_id`, then tricks a real user into completing
it on Microsoft's genuine login page; the resulting token goes to the
attacker, not to blumkin. blumkin's requested scopes (`Mail.ReadWrite`,
`Mail.Send`, `Calendars.ReadWrite`, and optionally `OnlineMeetings.ReadWrite` /
`People.Read`) are sensitive enough that this is worth configuring against,
not just accepting. blumkin itself never uses device-code or ROPC flows -
only interactive browser sign-in (auth code + PKCE, `localhost` redirect) -
so the mitigations below cost nothing functionally:

- **Single-tenant, not multi-tenant.** Set "Supported account types" to
  accounts in *this organizational directory only*, and set `tenant_id` in
  `config.toml` to your tenant's specific GUID or verified domain - never
  `common` / `organizations` / `consumers`. This is a per-installation
  setting: each operator registers their own app in their own tenant, so
  restricting yours has no effect on anyone else's ability to run blumkin.
  It bounds who can even attempt to sign in to *this* registration to actual
  members of *your* tenant, rather than anyone on the internet.
- **Disable "Allow public client flows"** in the app registration's
  Authentication blade unless you specifically need device code / ROPC -
  blumkin's interactive browser flow does not require it. Turning it off
  closes off device-code-flow phishing against this registration entirely.
- **Restrict redirect URIs** to `http://localhost` (loopback) only, with no
  wildcards - this is what `InteractiveBrowserCredential` uses and all it
  needs.
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
