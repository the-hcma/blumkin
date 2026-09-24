"""Load Blumkin config from ~/.config/blumkin/config.toml."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import tomlkit
import tomlkit.exceptions

from blumkin.output import emit_warning
from blumkin.providers.kind import ProviderConfigError, ProviderKind, parse_provider_kind

CONFIRM_COOLDOWN_FLOOR_SECONDS = 5
DEFAULT_CONFIRM_COOLDOWN_SECONDS = 20
DEFAULT_GRAPH_TIMEOUT_SECONDS = 60.0
DEFAULT_RSVP_FRESHNESS_SECONDS = 300
DEFAULT_TOKEN_REVERIFY_AFTER = timedelta(hours=24)
RSVP_FRESHNESS_FLOOR_SECONDS = 30


@dataclass(frozen=True, slots=True)
class BlumkinConfig:
    client_id: str
    config_dir: Path
    default_tz: str
    email: str
    files_scopes: bool
    google_oauth_client_file: Path | None
    graph_timeout_seconds: float
    mail_signature: MailSignatureConfig
    preferences: PreferencesConfig
    profile: str
    provider: ProviderKind
    tags: tuple[str, ...]
    tenant_id: str
    wo1162425_scopes: bool
    # Microsoft only (issue #297). Explicit per-profile opt-in - never inferred
    # from `tenant_id`'s value - for whether this profile's Entra app
    # registration is a work/school org tenant or a personal Microsoft Account
    # (`consumers`/`common`). "personal" excludes Teams-only scopes
    # (Chat.Read, and the WO1162425 add-ons) from effective_scopes() and gates
    # chat/meeting/people-resolve skills closed instead of letting them hit a
    # Graph 400/403 - see docs/SECURITY-AT-A-GLANCE.md.
    account_type: str = "organizational"
    # Opt-in Microsoft add-on: Files.ReadWrite for `docs create` (uploads a .docx
    # to OneDrive). Separate from files_scopes, which only unlocks chat-file
    # *reads* - see docs/DECISIONS.md D10. Defaulted so existing construction
    # sites (and configs) do not have to name it.
    docs_scopes: bool = False
    # Optional toml overrides for the Google Desktop OAuth client's non-secret
    # endpoints (issue #368: centralize non-secret profile configuration in
    # config.toml instead of only the Desktop client JSON referenced by
    # `google_oauth_client_file`). Empty means "not set" - callers fall back to
    # the Desktop client JSON's own value, then blumkin's hardcoded default.
    # `client_secret` is not here: it still only ever comes from
    # `google_oauth_client_file` (issue #368's keychain follow-up covers it).
    google_auth_uri: str = ""
    google_redirect_uris: tuple[str, ...] = ()
    google_token_uri: str = ""
    # OS keychain vs. a plain 0600 file for the token cache / auth record /
    # Google token (issue #287). "auto" prefers the keyring extra when a real
    # backend is usable at runtime, silently falling back to the file
    # otherwise (headless Linux with no Secret Service, etc.); "keyring" /
    # "file" force one and warn once if "keyring" is unusable. See
    # blumkin.secret_store.
    token_storage: str = "auto"
    # How long a profile's agent-cached secret may be reused before the
    # next read must re-verify local presence again. `None` disables the
    # agent cache entirely for this profile (`token_reverify_after = 0` /
    # `"never"` in config.toml).
    token_reverify_after: timedelta | None = DEFAULT_TOKEN_REVERIFY_AFTER

    @property
    def auth_record_path(self) -> Path:
        return self.profile_dir / "auth_record.json"

    @property
    def compose_state_path(self) -> Path:
        return self.profile_dir / "compose_state.json"

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def created_docs_path(self) -> Path:
        return self.profile_dir / "created_docs.json"

    @property
    def google_token_path(self) -> Path:
        return self.profile_dir / "google_token.json"

    @property
    def mail_signature_state_path(self) -> Path:
        return self.profile_dir / "mail_signature_state.json"

    @property
    def profile_dir(self) -> Path:
        return self.config_dir / "profiles" / self.profile

    @property
    def read_state_path(self) -> Path:
        return self.profile_dir / "read_state.json"

    @property
    def token_cache_path(self) -> Path:
        return self.profile_dir / "msal_token_cache.json"


@dataclass(frozen=True, slots=True)
class MailSignatureConfig:
    """Optional mail signature rendered into draft/reply/forward bodies."""

    affiliation: str = ""
    # Manual override: the client itself (Outlook when the automatic probe cannot
    # run yet, Gmail's own send-as signature, or any client not covered by
    # providers.microsoft_mail_probe) already appends a signature, so blumkin's own
    # [mail.signature] must stand down unconditionally for this profile. The probe
    # in providers/microsoft_mail_probe.py only covers Outlook's *new message*
    # signature on a Microsoft profile that has already run `auth login`; this
    # flag covers every other case (Google, reply/forward before issue #231 lands,
    # or before the probe has ever run) - see issue #256.
    client_appends_signature: bool = False
    enabled: bool = False
    html_template: str | None = None
    name: str = ""
    name_color: str = "#003366"
    title: str = ""
    title_color: str = "#5B9BD5"


@dataclass(frozen=True, slots=True)
class PreferencesConfig:
    """Display preferences for composed mail: a top-level default, per-profile override."""

    # Minimum wall-clock time (seconds) a notifying skill must sit composed
    # before `emit` (`mail.send-draft`, ...) will act on it - see issue #365.
    # `BLUMKIN_CONFIRM_COOLDOWN_SECONDS` overrides this for CI/tests only; it
    # is never a per-call tool argument.
    confirm_cooldown_seconds: int = DEFAULT_CONFIRM_COOLDOWN_SECONDS
    font_name: str = ""
    font_size: int | None = None
    html_email: bool = True
    # Maximum age (seconds) a `calendar.get` read of a given event may be before
    # `calendar.accept` / `decline` / `tentative` / `cancel` on that same event
    # refuse to act - see issue #365. The agent must look at current, unstale
    # event data (attendees, time, cancellation status) immediately before an
    # RSVP/cancel, not act on a stale id from earlier in the conversation.
    # `BLUMKIN_RSVP_FRESHNESS_SECONDS` overrides this for CI/tests only; it is
    # never a per-call tool argument.
    rsvp_freshness_seconds: int = DEFAULT_RSVP_FRESHNESS_SECONDS


def config_dir() -> Path:
    """Resolve the config directory (``BLUMKIN_CONFIG_DIR`` selects which dir)."""
    override = os.environ.get("BLUMKIN_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "blumkin"
    return Path.home() / ".config" / "blumkin"


def google_oauth_installed_client(path: Path) -> dict[str, Any]:
    """Return the ``installed`` (or ``web``) object from a Desktop client JSON."""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ProviderConfigError(f"cannot read google_oauth_client_file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderConfigError(f"google_oauth_client_file {path} must be a JSON object")
    for key in ("installed", "web"):
        section = data.get(key)
        if isinstance(section, dict):
            return section
    raise ProviderConfigError(
        f"google_oauth_client_file {path} missing installed/web client object"
    )


def list_profiles() -> list[dict[str, Any]]:
    """Return safe summaries of configured profiles (no secrets).

    Loading the provider, resolving tags, resolving the Google OAuth client
    id, parsing preferences, parsing ``token_reverify_after``, and checking
    ``auth_present`` are six independent ways a single profile's table can be
    malformed; each gets its own try/except so a failure in one does not
    suppress the others - a bad ``provider`` typo must not blank out an
    otherwise valid ``auth_present``, and no single misconfigured profile
    aborts the whole listing (issue #293). Each of these mirrors a validation
    ``load_config()`` itself performs per-profile table - google_oauth
    client id resolution (``_client_id_from_google_oauth_file``),
    ``[profiles.<name>.preferences]`` parsing (``_preferences_config``), and
    ``token_reverify_after`` parsing (``_token_reverify_after``, which can
    now also reject a value over the 1-week cap, PR #346 review) can each
    raise ``ProviderConfigError`` too, and must be just as tolerated here as
    ``provider``/``tags`` (issue #293 review). ``auth_present`` is computed
    via ``_auth_present_probe_cfg`` rather than the full
    ``load_config(profile=name)`` used elsewhere, specifically so it stays
    accurate even for a profile whose ``provider``/``tags``/
    ``token_reverify_after`` are invalid - the credential's on-disk location
    never depended on any of them being valid (issue #293); the probe cfg
    always hardcodes ``token_reverify_after=None`` rather than parsing it
    for that same reason. ``load_config()`` itself is never called here -
    doing so would reach ``_resolve_by_selector``, which scans *every*
    profile's tags to detect name/tag collisions, reintroducing the very
    "one broken profile hides every other profile" bug this function exists
    to avoid.
    """
    # Local import: blumkin.secret_store imports BlumkinConfig from this module,
    # so importing it at module level here would be circular.
    from blumkin import secret_store

    directory = config_dir()
    file_data = _read_toml(directory / "config.toml")
    tables, default_name = _profile_tables(file_data)
    marked_default = _configured_default_name(tables, default_name)
    summaries: list[dict[str, Any]] = []
    for name in sorted(tables):
        table = tables[name]
        provider = ""
        tags: tuple[str, ...] = ()
        errors: list[str] = []

        probe_cfg = _auth_present_probe_cfg(directory, name, table)
        ms_present = secret_store.ms_bundle_exists(probe_cfg)
        auth_present = {
            "auth_record": ms_present["auth_record"],
            "google_token": secret_store.exists(probe_cfg, "google_token"),
            "msal_token_cache": ms_present["token_cache"],
        }

        try:
            provider = _provider_kind(table).value
        except ProviderConfigError as exc:
            errors.append(str(exc))

        try:
            tags = _tags_from_table(table)
        except ProviderConfigError as exc:
            errors.append(str(exc))

        try:
            google_oauth_client_file = _google_oauth_client_file(table)
            client_id = _string_values(table).get("client_id", "").strip()
            if not client_id and google_oauth_client_file is not None:
                _client_id_from_google_oauth_file(google_oauth_client_file)
        except ProviderConfigError as exc:
            errors.append(str(exc))

        try:
            _preferences_config(table, _top_level_preferences(file_data), profile=name)
        except ProviderConfigError as exc:
            errors.append(str(exc))

        try:
            _token_reverify_after(table)
        except ProviderConfigError as exc:
            errors.append(str(exc))

        try:
            _account_type(table)
        except ProviderConfigError as exc:
            errors.append(str(exc))

        summary: dict[str, Any] = {
            "auth_present": auth_present,
            "default_tz": _string_values(table).get("default_tz", "").strip(),
            "email": _string_values(table).get("email", "").strip(),
            "is_default": name == marked_default,
            "name": name,
            "provider": provider,
            "tags": list(tags),
        }
        if errors:
            summary["error"] = "; ".join(errors)
        summaries.append(summary)
    return summaries


def profile_probe_config(name: str) -> BlumkinConfig:
    """Public entry point for the same minimal, secrets-only probe config
    ``list_profiles`` builds internally via ``_auth_present_probe_cfg`` -
    exposed so ``blumkin uninstall``'s keyring category can locate and
    remove a profile's keyring/Keychain items without needing the profile's
    full config (``provider``, etc.) to validate first (mirrors the
    ``list_profiles`` issue #293 rationale: a malformed profile must not
    block cleanup of its still-real, on-disk/keyring secret).
    """
    directory = config_dir()
    file_data = _read_toml(directory / "config.toml")
    tables, _ = _profile_tables(file_data)
    table = tables.get(name, {})
    return _auth_present_probe_cfg(directory, name, table)


def load_config(*, profile: str | None = None) -> BlumkinConfig:
    """Return config from ``config.toml`` only (no credential env overrides).

    ``BLUMKIN_CONFIG_DIR`` / ``XDG_CONFIG_HOME`` select which directory is used.
    ``profile`` (or ``BLUMKIN_PROFILE``) selects a profile name or unique tag.
    Google Desktop OAuth client id/secret come from ``google_oauth_client_file``
    (path in toml to the Cloud Console download JSON), not from env or toml
    plaintext secrets.
    """
    directory = config_dir()
    file_data = _read_toml(directory / "config.toml")
    tables, default_name = _profile_tables(file_data)
    selected = _resolve_profile_name(
        tables,
        default_name=default_name,
        explicit=profile,
    )
    table = tables[selected]
    string_values = _string_values(table)
    google_oauth_client_file = _google_oauth_client_file(table)
    client_id = string_values.get("client_id", "").strip()
    if not client_id and google_oauth_client_file is not None:
        client_id = _client_id_from_google_oauth_file(google_oauth_client_file)
    return BlumkinConfig(
        account_type=_account_type(table),
        client_id=client_id,
        config_dir=directory,
        default_tz=string_values.get("default_tz", "").strip(),
        docs_scopes=_docs_scopes_enabled(table),
        email=string_values.get("email", "").strip(),
        files_scopes=_files_scopes_enabled(table),
        google_auth_uri=_google_auth_uri(table),
        google_oauth_client_file=google_oauth_client_file,
        google_redirect_uris=_google_redirect_uris(table),
        google_token_uri=_google_token_uri(table),
        graph_timeout_seconds=_graph_timeout_seconds(table),
        mail_signature=_mail_signature_config(table),
        preferences=_preferences_config(table, _top_level_preferences(file_data), profile=selected),
        profile=selected,
        provider=_provider_kind(table),
        tags=_tags_from_table(table),
        tenant_id=string_values.get("tenant_id", "").strip(),
        token_reverify_after=_token_reverify_after(table),
        token_storage=_token_storage_preference(table),
        wo1162425_scopes=_wo1162425_scopes_enabled(table),
    )


def set_profile_email(
    config_path: Path,
    *,
    profile: str,
    email: str,
    overwrite: bool = False,
) -> bool:
    """Write ``email`` into one profile table.

    By default only when that key is absent - the automatic paths (login,
    refresh) fill a blank in, they never relabel a profile behind the operator's
    back. ``overwrite=True`` is for ``profiles set-email``, where replacing the
    value is the explicit ask.

    A style-preserving edit via ``tomlkit`` rather than a full re-serialize:
    config.toml is hand-maintained (comments, key order, the signature
    sub-table), and this runs once per profile at onboarding, so a fresh,
    reformatted document would blow away all of that for one display-only key.
    ``tomlkit`` gets this for free - it round-trips the untouched parts of the
    document exactly as written and only the assigned key changes.

    Returns True when the key was written, False when it already exists, the
    section is missing, or the value is empty. Never raises on an unwritable or
    unparseable file - the caller treats this as best-effort.
    """
    value = email.strip()
    if not value or not config_path.is_file():
        return False
    if any(ch == "\x7f" or (ord(ch) < 0x20 and ch != "\t") for ch in value):
        # A TOML basic string cannot carry a literal newline or control char, and no
        # real address does either. Refusing beats writing a file that then fails to
        # parse on every later command - a newline could even inject a table header.
        raise ValueError("email must not contain control characters or newlines")
    try:
        doc = tomlkit.parse(config_path.read_text())
    except OSError, tomlkit.exceptions.TOMLKitError:
        return False
    profiles = doc.get("profiles")
    if not isinstance(profiles, dict) or profile not in profiles:
        return False
    table = profiles[profile]
    if not isinstance(table, dict):
        return False
    existing = table.get("email")
    if existing is not None and not overwrite and str(existing).strip():
        # Already populated — the automatic paths never relabel a profile. An
        # empty value is a blank to fill, not a label to protect, so the two
        # guards agree with _populate_profile_email_once's `if cfg.email`.
        return False
    table["email"] = value
    try:
        config_path.write_text(tomlkit.dumps(doc))
    except OSError:
        return False
    return True


def set_profile_fields(
    config_path: Path,
    *,
    profile: str,
    fields: dict[str, str | list[str]],
) -> None:
    """Write non-secret ``fields`` into one profile table (issue #368 guided setup).

    A style-preserving edit via ``tomlkit`` - see ``set_profile_email`` for why.
    Unlike ``set_profile_email``, this always overwrites: it backs `blumkin auth
    setup`, where replacing a stale/incorrect value with the one the operator
    just supplied and validated is the explicit ask, not an automatic
    fill-in-a-blank path. Never write a secret through this function - it has
    no notion of "this key is sensitive" and would happily round-trip one into
    plaintext toml.

    Raises ``ProviderConfigError`` (never a silent no-op) when the profile
    table is missing or the file cannot be parsed/written: unlike the
    automatic ``set_profile_email`` fill-in, this only ever runs from an
    explicit operator action, so silently discarding the write would leave
    `auth setup` claiming success while nothing was actually persisted.
    """
    if not config_path.is_file():
        raise ProviderConfigError(f"{config_path} does not exist - create the profile first.")
    try:
        doc = tomlkit.parse(config_path.read_text())
    except (OSError, tomlkit.exceptions.TOMLKitError) as exc:
        raise ProviderConfigError(f"could not parse {config_path}: {exc}") from exc
    profiles = doc.get("profiles")
    if not isinstance(profiles, dict) or profile not in profiles:
        raise ProviderConfigError(f"profile {profile!r} not found in {config_path}")
    table = profiles[profile]
    if not isinstance(table, dict):
        raise ProviderConfigError(f"[profiles.{profile}] is not a table in {config_path}")
    for key, value in fields.items():
        table[key] = value
    try:
        config_path.write_text(tomlkit.dumps(doc))
    except OSError as exc:
        raise ProviderConfigError(f"could not write {config_path}: {exc}") from exc


def _auth_present_probe_cfg(directory: Path, profile: str, table: dict[str, Any]) -> BlumkinConfig:
    """Build a minimal ``BlumkinConfig`` sufficient to check ``auth_present``, nothing else.

    ``secret_store.exists()`` only ever consults ``cfg.token_storage``,
    ``cfg.config_dir``, ``cfg.profile``, and the path properties derived
    solely from ``config_dir``/``profile`` (``auth_record_path``,
    ``google_token_path``, ``token_cache_path``) - none of which depend on
    ``provider`` being valid. Building the *full* config via
    ``load_config(profile=name)`` instead would raise ``ProviderConfigError``
    for a profile whose ``provider`` has a typo, even though its on-disk
    credential is completely unaffected - reporting `auth_present: false` (or
    aborting the whole `list_profiles()` call) for a profile that is, in
    fact, still fully logged in (issue #293). Every field this probe cfg does
    not need is filled with a cheap, valid placeholder purely to satisfy the
    dataclass's required arguments. ``token_reverify_after`` is hardcoded to
    ``None`` rather than parsed from ``table`` for the same reason: this
    probe is built outside ``list_profiles()``'s per-field try/except
    guards, so an invalid or over-the-cap value (``_token_reverify_after``
    can raise ``ProviderConfigError``) would abort the whole listing instead
    of just that one profile's summary - and the agent cache TTL is
    irrelevant to a plain on-disk/keyring existence check anyway.
    """
    return BlumkinConfig(
        account_type="organizational",
        client_id="",
        config_dir=directory,
        default_tz="",
        email="",
        files_scopes=False,
        google_oauth_client_file=None,
        graph_timeout_seconds=DEFAULT_GRAPH_TIMEOUT_SECONDS,
        mail_signature=MailSignatureConfig(),
        preferences=PreferencesConfig(),
        profile=profile,
        provider=ProviderKind.MICROSOFT,
        tags=(),
        tenant_id="",
        token_reverify_after=None,
        token_storage=_token_storage_preference(table),
        wo1162425_scopes=False,
    )


def _account_type(file_data: dict[str, Any]) -> str:
    """Parse ``account_type`` (issue #297); explicit opt-in, never inferred from ``tenant_id``.

    Unlike ``token_storage`` (an implementation detail that silently falls
    back to "auto" on an unrecognized value), an invalid ``account_type`` is a
    security-relevant typo the operator needs to see immediately - a silent
    fallback to "organizational" here would mean a personal-account profile
    quietly keeps requesting Teams-only scopes it can never be granted.
    """
    if "account_type" not in file_data:
        return "organizational"
    raw = file_data["account_type"]
    if isinstance(raw, str) and raw.strip().lower() in {"organizational", "personal"}:
        return raw.strip().lower()
    raise ProviderConfigError(
        f"account_type must be 'organizational' or 'personal' in config.toml, got {raw!r}"
    )


def _client_id_from_google_oauth_file(path: Path) -> str:
    installed = google_oauth_installed_client(path)
    raw = installed.get("client_id")
    return raw.strip() if isinstance(raw, str) else ""


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return None


def _confirm_cooldown_seconds(merged: dict[str, Any]) -> int:
    """Resolve ``preferences.confirm_cooldown_seconds``, honoring the CI/test-only env override.

    Never read by a per-call tool argument - see the field docstring on
    :class:`PreferencesConfig`.
    """
    env_override = os.environ.get("BLUMKIN_CONFIRM_COOLDOWN_SECONDS", "").strip()
    if env_override:
        try:
            env_value = int(env_override)
        except ValueError as exc:
            raise ProviderConfigError(
                f"BLUMKIN_CONFIRM_COOLDOWN_SECONDS must be an integer, got {env_override!r}"
            ) from exc
        return _validate_cooldown_seconds(
            env_value,
            i_understand_the_risk=True,
            source="BLUMKIN_CONFIRM_COOLDOWN_SECONDS",
        )
    raw = merged.get("confirm_cooldown_seconds", DEFAULT_CONFIRM_COOLDOWN_SECONDS)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ProviderConfigError(
            f"preferences.confirm_cooldown_seconds must be an integer in config.toml, got {raw!r}"
        )
    understood = _coerce_bool(merged.get("i_understand_the_risk")) or False
    return _validate_cooldown_seconds(
        raw, i_understand_the_risk=understood, source="preferences.confirm_cooldown_seconds"
    )


def _configured_default_name(
    tables: dict[str, dict[str, Any]],
    default_name: str | None,
) -> str | None:
    if len(tables) == 1:
        return next(iter(tables))
    if default_name is not None and default_name in tables:
        return default_name
    return None


def _docs_scopes_enabled(file_data: dict[str, Any]) -> bool:
    if "docs_scopes" in file_data:
        coerced = _coerce_bool(file_data["docs_scopes"])
        if coerced is not None:
            return coerced
    return False


def _files_scopes_enabled(file_data: dict[str, Any]) -> bool:
    if "files_scopes" in file_data:
        coerced = _coerce_bool(file_data["files_scopes"])
        if coerced is not None:
            return coerced
    return False


def _google_auth_uri(file_data: dict[str, Any]) -> str:
    """Optional toml override for the Google OAuth authorization endpoint (issue #368).

    Empty string means "not set" - ``_client_config`` falls back to the
    Desktop client JSON's own ``auth_uri``, then blumkin's hardcoded default.
    """
    raw = file_data.get("google_auth_uri")
    if raw is None:
        return ""
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    raise ProviderConfigError(
        f"google_auth_uri must be a non-empty string in config.toml, got {raw!r}"
    )


def _google_oauth_client_file(file_data: dict[str, Any]) -> Path | None:
    raw = file_data.get("google_oauth_client_file")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw.strip()).expanduser()


def _google_redirect_uris(file_data: dict[str, Any]) -> tuple[str, ...]:
    """Optional toml override for the Google OAuth redirect URI(s) (issue #368).

    Empty tuple means "not set" - ``_client_config`` falls back to the
    Desktop client JSON's own ``redirect_uris``, then blumkin's hardcoded
    ``["http://localhost"]``.
    """
    raw = file_data.get("google_redirect_uris")
    if raw is None:
        return ()
    if (
        isinstance(raw, list)
        and raw
        and all(isinstance(item, str) and item.strip() for item in raw)
    ):
        return tuple(item.strip() for item in raw)
    raise ProviderConfigError(
        f"google_redirect_uris must be a list of non-empty strings in config.toml, got {raw!r}"
    )


def _google_token_uri(file_data: dict[str, Any]) -> str:
    """Optional toml override for the Google OAuth token endpoint (issue #368).

    Empty string means "not set" - ``_client_config`` falls back to the
    Desktop client JSON's own ``token_uri``, then blumkin's hardcoded default.
    """
    raw = file_data.get("google_token_uri")
    if raw is None:
        return ""
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    raise ProviderConfigError(
        f"google_token_uri must be a non-empty string in config.toml, got {raw!r}"
    )


def _graph_timeout_seconds(file_data: dict[str, Any]) -> float:
    raw = file_data.get("graph_timeout_seconds")
    if isinstance(raw, int | float) and not isinstance(raw, bool) and float(raw) > 0:
        return float(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            value = float(raw.strip())
        except ValueError:
            return DEFAULT_GRAPH_TIMEOUT_SECONDS
        if value > 0:
            return value
    return DEFAULT_GRAPH_TIMEOUT_SECONDS


def _mail_signature_config(file_data: dict[str, Any]) -> MailSignatureConfig:
    """Parse optional ``[mail.signature]`` (nested table under ``mail``)."""
    mail = file_data.get("mail")
    if not isinstance(mail, dict):
        return MailSignatureConfig()
    raw = mail.get("signature")
    if not isinstance(raw, dict):
        return MailSignatureConfig()
    client_appends_signature = _coerce_bool(raw.get("client_appends_signature"))
    enabled = _coerce_bool(raw.get("enabled"))
    return MailSignatureConfig(
        affiliation=str(raw.get("affiliation") or "").strip(),
        client_appends_signature=(
            bool(client_appends_signature) if client_appends_signature is not None else False
        ),
        enabled=bool(enabled) if enabled is not None else False,
        html_template=_optional_str(raw.get("html_template")),
        name=str(raw.get("name") or "").strip(),
        name_color=str(raw.get("name_color") or "#003366").strip() or "#003366",
        title=str(raw.get("title") or "").strip(),
        title_color=str(raw.get("title_color") or "#5B9BD5").strip() or "#5B9BD5",
    )


def _normalize_selector(value: str) -> str:
    text = value.strip().lower()
    if text.startswith("@"):
        return text[1:]
    return text


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive_int(value: Any, *, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProviderConfigError(f"{key} must be a positive integer in config.toml, got {value!r}")
    return value


def _preferences_config(
    table: dict[str, Any], top_level: dict[str, Any], *, profile: str
) -> PreferencesConfig:
    """Merge ``[profiles.<name>.preferences]`` over the top-level ``[preferences]`` default.

    A key set at both scopes with different values still resolves to the profile's
    value (the override is intentional), but is worth flagging - it may be drift
    rather than a deliberate per-profile tweak.
    """
    raw = table.get("preferences")
    if raw is not None and not isinstance(raw, dict):
        raise ProviderConfigError(
            f"profiles.{profile}.preferences must be a table in config.toml, "
            f"got {type(raw).__name__}"
        )
    profile_prefs: dict[str, Any] = raw or {}
    for key in sorted(profile_prefs):
        if key in top_level and profile_prefs[key] != top_level[key]:
            emit_warning(
                f"profile {profile!r} sets preferences.{key} = {profile_prefs[key]!r}, "
                f"overriding preferences.{key} = {top_level[key]!r} set at the top level"
            )
    merged = {**top_level, **profile_prefs}
    html_email = True
    if "html_email" in merged:
        raw_html_email = merged["html_email"]
        # Strict bool, unlike the lenient _coerce_bool used elsewhere in this file:
        # this flag silently switches the wire format (HTML vs. plain text) that
        # every composed message sends, so a stray "true"/1 typo should fail loudly
        # rather than quietly do the right thing today and the wrong thing tomorrow.
        if not isinstance(raw_html_email, bool):
            raise ProviderConfigError(
                f"preferences.html_email must be a boolean in config.toml, got {raw_html_email!r}"
            )
        html_email = raw_html_email
    return PreferencesConfig(
        confirm_cooldown_seconds=_confirm_cooldown_seconds(merged),
        font_name=str(merged.get("font_name") or "").strip(),
        font_size=_positive_int(merged.get("font_size"), key="preferences.font_size"),
        html_email=html_email,
        rsvp_freshness_seconds=_rsvp_freshness_seconds(merged),
    )


def _profile_tables(
    file_data: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Return (name → table, default_profile name)."""
    if "profiles" in file_data:
        raw_profiles = file_data["profiles"]
        if not isinstance(raw_profiles, dict):
            raise ProviderConfigError(
                f"profiles must be a table in config.toml, got {type(raw_profiles).__name__}"
            )
        if not raw_profiles:
            raise ProviderConfigError(
                "profiles table is empty; add [profiles.<name>] entries or remove the key"
            )
        stray = sorted(
            key for key in file_data if key not in {"default_profile", "preferences", "profiles"}
        )
        if stray:
            shown = ", ".join(stray)
            raise ProviderConfigError(
                "named [profiles.*] layout cannot mix top-level flat keys "
                f"({shown}); move them under [profiles.<name>] or remove [profiles]"
            )
        tables: dict[str, dict[str, Any]] = {}
        seen_lower: dict[str, str] = {}
        for name, table in raw_profiles.items():
            if not isinstance(name, str) or not name.strip():
                raise ProviderConfigError("profile names must be non-empty strings")
            cleaned = name.strip()
            if cleaned in {".", ".."} or any(sep in cleaned for sep in ("/", "\\")):
                raise ProviderConfigError(
                    f"profile name {cleaned!r} must be a single path segment "
                    "(no slashes, '.', or '..')"
                )
            folded = cleaned.casefold()
            prior = seen_lower.get(folded)
            if prior is not None:
                raise ProviderConfigError(
                    f"profile names {prior!r} and {cleaned!r} collide on "
                    "case-insensitive filesystems; rename one"
                )
            if not isinstance(table, dict):
                raise ProviderConfigError(
                    f"profiles.{cleaned} must be a table in config.toml, got {type(table).__name__}"
                )
            seen_lower[folded] = cleaned
            tables[cleaned] = table
        default_raw = file_data.get("default_profile")
        default_name: str | None = None
        if isinstance(default_raw, str) and default_raw.strip():
            default_name = default_raw.strip()
        elif default_raw is not None:
            raise ProviderConfigError(
                f"default_profile must be a string in config.toml, got {type(default_raw).__name__}"
            )
        return tables, default_name

    # Missing / empty config.toml: no profiles (do not invent a phantom "default").
    if not file_data:
        return {}, None

    # default_profile alone must not invent a phantom empty "default" profile.
    if set(file_data) <= {"default_profile"}:
        raise ProviderConfigError(
            "default_profile set but no profiles configured; add [profiles.<name>] entries"
        )

    # Flat top-level keys with no [profiles.*] table are no longer a valid layout -
    # config.toml used to fold them into one implicit "default" profile, but that made
    # every consumer (profile_dir, set_profile_email, ...) carry a legacy_flat branch
    # for a shape only ever seen on first-time setup. See README.md for the profile
    # layout this now requires.
    stray = sorted(key for key in file_data if key != "default_profile")
    raise ProviderConfigError(
        "config.toml must use [profiles.<name>]; flat top-level keys "
        f"({', '.join(stray)}) are no longer supported - see README.md"
    )


def _provider_kind(file_data: dict[str, Any]) -> ProviderKind:
    if "provider" not in file_data:
        return ProviderKind.MICROSOFT
    raw = file_data["provider"]
    if isinstance(raw, str):
        return parse_provider_kind(raw)
    raise ProviderConfigError(f"provider must be a string in config.toml, got {type(raw).__name__}")


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text())


def _rsvp_freshness_seconds(merged: dict[str, Any]) -> int:
    """Resolve ``preferences.rsvp_freshness_seconds``, honoring the CI/test-only env override.

    Never read by a per-call tool argument - see the field docstring on
    :class:`PreferencesConfig`.
    """
    env_override = os.environ.get("BLUMKIN_RSVP_FRESHNESS_SECONDS", "").strip()
    if env_override:
        try:
            env_value = int(env_override)
        except ValueError as exc:
            raise ProviderConfigError(
                f"BLUMKIN_RSVP_FRESHNESS_SECONDS must be an integer, got {env_override!r}"
            ) from exc
        return _validate_cooldown_seconds(
            env_value,
            i_understand_the_risk=True,
            source="BLUMKIN_RSVP_FRESHNESS_SECONDS",
            floor=RSVP_FRESHNESS_FLOOR_SECONDS,
            disabled_description="the mandatory RSVP/cancel freshness check",
        )
    raw = merged.get("rsvp_freshness_seconds", DEFAULT_RSVP_FRESHNESS_SECONDS)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ProviderConfigError(
            f"preferences.rsvp_freshness_seconds must be an integer in config.toml, got {raw!r}"
        )
    understood = _coerce_bool(merged.get("i_understand_the_risk")) or False
    return _validate_cooldown_seconds(
        raw,
        i_understand_the_risk=understood,
        source="preferences.rsvp_freshness_seconds",
        floor=RSVP_FRESHNESS_FLOOR_SECONDS,
        disabled_description="the mandatory RSVP/cancel freshness check",
    )


def _resolve_by_selector(
    tables: dict[str, dict[str, Any]],
    selector: str,
    *,
    source: str,
) -> str:
    tag_only = selector.startswith("@")
    needle = _normalize_selector(selector)
    if not needle:
        raise ProviderConfigError(f"{source} is empty; choose a profile name or tag")
    if tag_only:
        name_matches: list[str] = []
    elif selector in tables:
        name_matches = [selector]
    else:
        name_matches = [name for name in tables if _normalize_selector(name) == needle]
    tag_matches = [
        name
        for name, table in tables.items()
        if any(_normalize_selector(tag) == needle for tag in _tags_from_table(table))
    ]
    other_tag_matches = [name for name in tag_matches if name not in name_matches]
    if name_matches and other_tag_matches:
        collided = ", ".join(sorted({*name_matches, *other_tag_matches}))
        raise ProviderConfigError(
            f"{source} {selector!r} matches multiple profiles by name/tag: {collided}"
        )
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        names = ", ".join(sorted(name_matches))
        raise ProviderConfigError(f"{source} {selector!r} matches multiple profile names: {names}")
    unique_tags = sorted(set(tag_matches))
    if len(unique_tags) == 1:
        return unique_tags[0]
    if len(unique_tags) > 1:
        raise ProviderConfigError(
            f"{source} {selector!r} matches multiple profiles by tag: {', '.join(unique_tags)}"
        )
    available = ", ".join(sorted(tables)) or "(none)"
    raise ProviderConfigError(
        f"{source} {selector!r} matches no profile name or unique tag; available: {available}"
    )


def _resolve_profile_name(
    tables: dict[str, dict[str, Any]],
    *,
    default_name: str | None,
    explicit: str | None,
) -> str:
    if not tables:
        raise ProviderConfigError("no profiles configured in config.toml")
    if explicit is not None and explicit.strip():
        return _resolve_by_selector(tables, explicit.strip(), source="profile")
    env_raw = os.environ.get("BLUMKIN_PROFILE", "").strip()
    if env_raw:
        return _resolve_by_selector(tables, env_raw, source="BLUMKIN_PROFILE")
    if len(tables) == 1:
        return next(iter(tables))
    if default_name is not None:
        if default_name in tables:
            return default_name
        available = ", ".join(sorted(tables))
        raise ProviderConfigError(
            f"default_profile {default_name!r} is not a configured profile; available: {available}"
        )
    available = ", ".join(sorted(tables))
    raise ProviderConfigError(
        "multiple profiles configured; pass --profile / BLUMKIN_PROFILE, or set "
        f"default_profile; available: {available}"
    )


def _string_values(file_data: dict[str, Any]) -> dict[str, str]:
    return {key: value for key, value in file_data.items() if isinstance(value, str)}


def _tags_from_table(table: dict[str, Any]) -> tuple[str, ...]:
    raw = table.get("tags")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProviderConfigError(
            f"tags must be a list of strings in config.toml, got {type(raw).__name__}"
        )
    seen: set[str] = set()
    tags: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ProviderConfigError("tags entries must be non-empty strings")
        text = item.strip()
        if text in seen:
            continue
        seen.add(text)
        tags.append(text)
    return tuple(sorted(tags, key=str.lower))


def _top_level_preferences(file_data: dict[str, Any]) -> dict[str, Any]:
    """The optional top-level ``[preferences]`` table, applying to every profile."""
    raw = file_data.get("preferences")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProviderConfigError(
            f"preferences must be a table in config.toml, got {type(raw).__name__}"
        )
    return raw


_TOKEN_REVERIFY_AFTER_RE = re.compile(r"^(\d+)\s*([mhdw])$", re.IGNORECASE)

#: The `blumkin-agent` daemon exits after this long with no requests (its own
#: `IDLE_EXIT_SECONDS`, `rust-agent/src/server.rs`) so an abandoned agent never
#: lingers forever - and that idle window is only kept comfortably above this
#: cap, not above an arbitrary caller-chosen TTL. A `token_reverify_after`
#: longer than the agent is guaranteed to stay alive for would let an
#: unrelated idle-exit silently drop an still-in-TTL cached secret early
#: (review finding on PR #346), so this is enforced here rather than only
#: documented as a suggested maximum.
_MAX_TOKEN_REVERIFY_AFTER = timedelta(weeks=1)


def _token_reverify_after(file_data: dict[str, Any]) -> timedelta | None:
    """Parse ``token_reverify_after``.

    Accepts the same compact duration forms blumkin already uses elsewhere
    (`30m`, `24h`, `7d`, `1w`; `1w` is the maximum - see
    `_MAX_TOKEN_REVERIFY_AFTER`). `0`/`"never"` disable agent-mode for this
    profile entirely.
    """
    raw = file_data.get("token_reverify_after")
    if raw is None:
        return DEFAULT_TOKEN_REVERIFY_AFTER
    if isinstance(raw, int) and not isinstance(raw, bool):
        if raw == 0:
            return None
        raise ProviderConfigError(
            "token_reverify_after must be a duration like 30m/24h, or 0/never to disable"
        )
    if not isinstance(raw, str):
        raise ProviderConfigError(
            "token_reverify_after must be a string like 30m/24h, or 0/never to disable"
        )
    text = raw.strip().lower()
    if text in {"0", "never"}:
        return None
    match = _TOKEN_REVERIFY_AFTER_RE.fullmatch(text)
    if match is None:
        raise ProviderConfigError(
            f"invalid token_reverify_after {raw!r}; use forms like 30m, 24h, 7d, 1w, or 0/never"
        )
    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        raise ProviderConfigError("token_reverify_after must be positive, or 0/never to disable")
    try:
        if unit == "w":
            value = timedelta(weeks=amount)
        elif unit == "d":
            value = timedelta(days=amount)
        elif unit == "h":
            value = timedelta(hours=amount)
        else:
            value = timedelta(minutes=amount)
    except OverflowError:
        # An absurdly large amount (e.g. "1000000000w") would otherwise raise
        # timedelta's own OverflowError before the cap check below runs,
        # surfacing an unclassified traceback instead of the friendly
        # ProviderConfigError every other invalid form gets.
        raise ProviderConfigError(
            f"token_reverify_after {raw!r} exceeds the maximum of 1w "
            "(the agent's idle-exit window only covers up to that long)"
        ) from None
    if value > _MAX_TOKEN_REVERIFY_AFTER:
        raise ProviderConfigError(
            f"token_reverify_after {raw!r} exceeds the maximum of 1w "
            "(the agent's idle-exit window only covers up to that long)"
        )
    return value


def _token_storage_preference(file_data: dict[str, Any]) -> str:
    """Parse ``token_storage`` (issue #287); any unrecognized value is "auto"."""
    raw = file_data.get("token_storage")
    if isinstance(raw, str) and raw.strip().lower() in {"auto", "file", "keyring"}:
        return raw.strip().lower()
    return "auto"


def _validate_cooldown_seconds(
    value: int,
    *,
    i_understand_the_risk: bool,
    source: str,
    floor: int = CONFIRM_COOLDOWN_FLOOR_SECONDS,
    disabled_description: str = "the mandatory send/emit confirmation cooldown",
) -> int:
    """Enforce a floor on a "must dwell at least N seconds" preference - see issue #365.

    Shared by ``confirm_cooldown_seconds`` and ``rsvp_freshness_seconds``: ``0``
    (disabled) requires an explicit ``i_understand_the_risk = true`` sibling key,
    so the mandatory pause can't be silently switched off by a stray ``0``. Any
    other value below ``floor`` is always rejected outright.
    """
    if value == 0:
        if not i_understand_the_risk:
            raise ProviderConfigError(
                f"{source} = 0 disables {disabled_description}; "
                "add `i_understand_the_risk = true` alongside it to confirm this is intentional"
            )
        return 0
    if value < floor:
        raise ProviderConfigError(
            f"{source} must be 0 (disabled, with i_understand_the_risk = true) or at least "
            f"{floor} in config.toml, got {value!r}"
        )
    return value


def _wo1162425_scopes_enabled(file_data: dict[str, Any]) -> bool:
    if "wo1162425_scopes" in file_data:
        coerced = _coerce_bool(file_data["wo1162425_scopes"])
        if coerced is not None:
            return coerced
    return False
