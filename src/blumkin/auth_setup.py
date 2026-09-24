"""Guided OAuth app-registration setup for `blumkin auth setup` (issue #368).

Google's `client_secret` and Microsoft's `client_id` are only ever obtained by
the operator registering an app in the respective cloud console - blumkin
cannot create that registration for them. This module owns the two things it
*can* do on their behalf once they have the console open in front of them:

1. Tell them exactly what to click, in order (`console_steps`).
2. Validate the values they paste back before anything is written
   (`validate_google_setup` / `validate_microsoft_setup`), then split them:
   secrets to the OS keychain (`blumkin.app_secrets`), non-secrets to
   `config.toml` (`blumkin.config.set_profile_fields`).

Deliberately thin: prompting/TTY handling and `--json`/exit-code plumbing stay
in `cli.py` (matching `mcp_install.py`'s split), so this module is exercised
with plain function calls in tests, no `CliRunner` needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from blumkin.app_secrets import _read_app_secret_or_raise, write_app_secret
from blumkin.config import BlumkinConfig, google_oauth_installed_client, set_profile_fields
from blumkin.providers.kind import ProviderConfigError
from blumkin.secret_store import SecretWriteError

ACCOUNT_TYPES: frozenset[str] = frozenset({"organizational", "personal"})

GOOGLE_CONSOLE_STEPS: tuple[str, ...] = (
    "Open https://console.cloud.google.com/ and select (or create) a project.",
    "APIs & Services -> Library: enable the Google Calendar API and the Gmail API.",
    "APIs & Services -> OAuth consent screen: choose Internal (Workspace org) or "
    "External + Testing (personal Gmail), add your own account under Test users "
    "if External, and skip Publish/verification for personal use.",
    "APIs & Services -> Credentials -> Create credentials -> OAuth client ID -> "
    "Application type: Desktop app -> Create.",
    "Download JSON immediately - the client_secret is shown once and cannot be "
    "re-downloaded later. If the download button is missing, copy the Client ID "
    "and Client secret from the creation dialog before closing it.",
)


@dataclass(frozen=True, slots=True)
class GoogleSetupInput:
    """Values collected for a Google profile, before validation/writing."""

    client_id: str
    client_secret: str
    auth_uri: str = ""
    redirect_uris: tuple[str, ...] = ()
    token_uri: str = ""


MICROSOFT_CONSOLE_STEPS: tuple[str, ...] = (
    "Open https://entra.microsoft.com/ (or the Azure Portal -> Microsoft Entra ID).",
    "Identity -> Applications -> App registrations -> New registration.",
    "Name it (for example `blumkin`); under Supported account types, pick the "
    "option matching your account (a single org tenant, vs. personal Microsoft "
    "accounts only).",
    "Redirect URI: platform Public client/native, value http://localhost.",
    "After Register, copy the Application (client) ID and the Directory "
    "(tenant) ID from the app's Overview page.",
    "API permissions: add the delegated Microsoft Graph scopes blumkin requests "
    "(see docs/google-setup.md's Microsoft counterpart, or docs/DECISIONS.md) - "
    "admin consent only if your tenant requires it for those scopes.",
)


@dataclass(frozen=True, slots=True)
class MicrosoftSetupInput:
    """Values collected for a Microsoft profile, before validation/writing."""

    account_type: str
    client_id: str
    tenant_id: str


def apply_google_setup(cfg: BlumkinConfig, data: GoogleSetupInput) -> None:
    """Validate, write config.toml, then vault `client_secret`.

    config.toml is written first: if that raises (missing/unwritable file,
    concurrently-removed profile table, ...), nothing new lands in the
    keychain - the operator sees one clear error instead of a keychain
    secret whose config.toml side never landed.
    """
    validate_google_setup(data)
    fields: dict[str, str | list[str]] = {"client_id": data.client_id.strip()}
    if data.auth_uri:
        fields["google_auth_uri"] = data.auth_uri
    if data.token_uri:
        fields["google_token_uri"] = data.token_uri
    if data.redirect_uris:
        fields["google_redirect_uris"] = list(data.redirect_uris)
    set_profile_fields(cfg.config_path, profile=cfg.profile, fields=fields)
    write_app_secret(cfg, "google_client_secret", data.client_secret)


def apply_microsoft_setup(cfg: BlumkinConfig, data: MicrosoftSetupInput) -> bool:
    """Validate, write config.toml, then best-effort vault `client_id`.

    `client_id` is written to config.toml *and* vaulted: the vaulted copy
    wins when readable (`auth._resolved_client_id`), but the toml copy is
    the durable fallback on a machine/session where the keychain is not
    readable (`token_storage = "file"`, no keyring backend, a locked
    keychain, ...) - see docs/microsoft-personal-setup.md's "vault
    client_id instead of toml" note, which only ever makes the toml copy
    optional to *keep*, not something `auth setup` may skip writing.
    config.toml is written first - see `apply_google_setup` for why.

    Unlike Google's `client_secret` - which lives *only* in the keychain
    once vaulted, so a write failure there must fail the whole command -
    config.toml alone is already enough for Microsoft's profile to log in
    (`auth._resolved_client_id` falls back to `cfg.client_id`). So a
    `SecretWriteError` here (no usable keychain backend) is swallowed:
    returns ``False`` instead of raising, letting the caller report a
    non-fatal warning while still exiting 0 - the config.toml write above
    already made the profile fully functional (issue #368 review).
    """
    validate_microsoft_setup(data)
    set_profile_fields(
        cfg.config_path,
        profile=cfg.profile,
        fields={
            "client_id": data.client_id.strip(),
            "account_type": data.account_type,
            "tenant_id": data.tenant_id.strip(),
        },
    )
    try:
        write_app_secret(cfg, "ms_client_id", data.client_id.strip())
    except SecretWriteError as write_exc:
        # A readable vaulted value that differs from what config.toml now
        # says would shadow it (auth._resolved_client_id prefers the
        # keychain) - only the best-effort "vaulting failed, config.toml is
        # authoritative" story holds when there is nothing stale to shadow it
        # (issue #368 review).
        try:
            stale = _read_app_secret_or_raise(cfg, "ms_client_id")
        except Exception as read_exc:
            # The follow-up read failed too, so a stale vaulted value is
            # indistinguishable from nothing being vaulted - surface it
            # rather than report success (issue #384).
            raise SecretWriteError(
                f"could not vault ms_client_id ({write_exc}), and confirming "
                f"whether an older value is already vaulted also failed - "
                f"cannot rule out it shadowing the new client_id: {read_exc}"
            ) from write_exc
        if stale and stale != data.client_id.strip():
            raise
        return False
    return True


def console_steps(provider: str) -> tuple[str, ...]:
    """Ordered console instructions for ``provider`` ('google' or 'microsoft')."""
    if provider == "google":
        return GOOGLE_CONSOLE_STEPS
    if provider == "microsoft":
        return MICROSOFT_CONSOLE_STEPS
    raise ProviderConfigError(f"no setup walkthrough for provider {provider!r}")


def google_setup_from_client_json(path: Path) -> GoogleSetupInput:
    """Build a ``GoogleSetupInput`` from a downloaded Desktop client JSON.

    Extracts every field the console step's download already carries
    (`client_id`, `client_secret`, and the non-secret endpoints when present) so
    the operator does not have to retype them by hand.
    """
    installed = google_oauth_installed_client(path)
    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret")
    if not isinstance(client_id, str) or not client_id.strip():
        raise ProviderConfigError(f"{path} has no usable client_id")
    if not isinstance(client_secret, str) or not client_secret.strip():
        raise ProviderConfigError(f"{path} has no usable client_secret")
    auth_uri = installed.get("auth_uri")
    token_uri = installed.get("token_uri")
    redirect_uris = installed.get("redirect_uris")
    return GoogleSetupInput(
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
        auth_uri=auth_uri.strip() if isinstance(auth_uri, str) else "",
        redirect_uris=(
            tuple(item.strip() for item in redirect_uris if isinstance(item, str) and item.strip())
            if isinstance(redirect_uris, list)
            else ()
        ),
        token_uri=token_uri.strip() if isinstance(token_uri, str) else "",
    )


def validate_google_setup(data: GoogleSetupInput) -> None:
    """Raise ``ProviderConfigError`` (with a fix-it hint) on the first bad field."""
    client_id = data.client_id.strip()
    if not client_id:
        raise ProviderConfigError("client_id is required.")
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise ProviderConfigError(
            f"client_id {client_id!r} does not look like a Google OAuth client id "
            "(expected it to end with '.apps.googleusercontent.com') - re-check the "
            "value copied from the Cloud Console."
        )
    if len(data.client_secret.strip()) < 8:
        raise ProviderConfigError(
            "client_secret is missing or implausibly short - re-check the value "
            "copied from the Cloud Console (or re-download the Desktop client JSON)."
        )
    for label, value in (("auth_uri", data.auth_uri), ("token_uri", data.token_uri)):
        if value and not value.startswith("https://"):
            raise ProviderConfigError(f"{label} {value!r} must start with https://.")
    for uri in data.redirect_uris:
        if not uri.strip():
            raise ProviderConfigError("redirect_uris must not contain an empty value.")


def validate_microsoft_setup(data: MicrosoftSetupInput) -> None:
    """Raise ``ProviderConfigError`` (with a fix-it hint) on the first bad field."""
    if data.account_type not in ACCOUNT_TYPES:
        raise ProviderConfigError(
            f"account_type must be one of {sorted(ACCOUNT_TYPES)}, got {data.account_type!r}."
        )
    client_id = data.client_id.strip()
    if not _GUID_RE.match(client_id):
        raise ProviderConfigError(
            f"client_id {client_id!r} does not look like an Entra Application "
            "(client) ID GUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx) - re-check the "
            "value copied from the app's Overview page."
        )
    tenant_id = data.tenant_id.strip()
    if not tenant_id:
        raise ProviderConfigError("tenant_id is required.")
    lowered = tenant_id.lower()
    if data.account_type == "personal" and lowered not in {"common", "consumers"}:
        raise ProviderConfigError(
            f"account_type = 'personal' needs tenant_id = 'consumers' (or 'common'), "
            f"got {tenant_id!r} - a personal Microsoft account is never a directory GUID."
        )
    if data.account_type == "organizational" and lowered in {
        "common",
        "consumers",
        "organizations",
    }:
        raise ProviderConfigError(
            f"account_type = 'organizational' but tenant_id = {tenant_id!r} is a "
            "personal-account or multi-tenant reserved value - use your Entra "
            "tenant's specific GUID or verified domain instead (never 'common' / "
            "'organizations' / 'consumers'), so sign-in stays bounded to your own "
            "tenant per docs/SECURITY-AT-A-GLANCE.md, or set account_type = "
            "'personal' instead."
        )


_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
