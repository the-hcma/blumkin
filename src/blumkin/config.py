"""Load Blumkin config from ~/.config/blumkin/config.toml."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blumkin.providers.kind import ProviderConfigError, ProviderKind, parse_provider_kind

DEFAULT_GRAPH_TIMEOUT_SECONDS = 60.0
_LEGACY_PROFILE_NAME = "default"


@dataclass(frozen=True, slots=True)
class BlumkinConfig:
    client_id: str
    config_dir: Path
    default_tz: str
    email: str
    files_scopes: bool
    google_oauth_client_file: Path | None
    graph_timeout_seconds: float
    legacy_flat: bool
    mail_signature: MailSignatureConfig
    profile: str
    provider: ProviderKind
    tags: tuple[str, ...]
    tenant_id: str
    wo1162425_scopes: bool

    @property
    def auth_record_path(self) -> Path:
        return self.profile_dir / "auth_record.json"

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def google_token_path(self) -> Path:
        return self.profile_dir / "google_token.json"

    @property
    def profile_dir(self) -> Path:
        if self.legacy_flat:
            return self.config_dir
        return self.config_dir / "profiles" / self.profile

    @property
    def token_cache_path(self) -> Path:
        return self.profile_dir / "msal_token_cache.json"


@dataclass(frozen=True, slots=True)
class MailSignatureConfig:
    """Optional mail signature rendered into draft/reply/forward bodies."""

    affiliation: str = ""
    enabled: bool = False
    html_template: str | None = None
    name: str = ""
    name_color: str = "#003366"
    title: str = ""
    title_color: str = "#5B9BD5"


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
    """Return safe summaries of configured profiles (no secrets)."""
    directory = config_dir()
    file_data = _read_toml(directory / "config.toml")
    tables, default_name, legacy_flat = _profile_tables(file_data)
    marked_default = _configured_default_name(tables, default_name)
    summaries: list[dict[str, Any]] = []
    for name in sorted(tables):
        table = tables[name]
        profile_dir = directory if legacy_flat else directory / "profiles" / name
        tags = _tags_from_table(table)
        summaries.append(
            {
                "auth_present": {
                    "auth_record": (profile_dir / "auth_record.json").is_file(),
                    "google_token": (profile_dir / "google_token.json").is_file(),
                    "msal_token_cache": (profile_dir / "msal_token_cache.json").is_file(),
                },
                "default_tz": _string_values(table).get("default_tz", "").strip(),
                "email": _string_values(table).get("email", "").strip(),
                "is_default": name == marked_default,
                "name": name,
                "provider": _provider_kind(table).value,
                "tags": list(tags),
            }
        )
    return summaries


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
    tables, default_name, legacy_flat = _profile_tables(file_data)
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
        client_id=client_id,
        config_dir=directory,
        default_tz=string_values.get("default_tz", "").strip(),
        email=string_values.get("email", "").strip(),
        files_scopes=_files_scopes_enabled(table),
        google_oauth_client_file=google_oauth_client_file,
        graph_timeout_seconds=_graph_timeout_seconds(table),
        legacy_flat=legacy_flat,
        mail_signature=_mail_signature_config(table),
        profile=selected,
        provider=_provider_kind(table),
        tags=_tags_from_table(table),
        tenant_id=string_values.get("tenant_id", "").strip(),
        wo1162425_scopes=_wo1162425_scopes_enabled(table),
    )


def set_profile_email(
    config_path: Path,
    *,
    profile: str,
    email: str,
    legacy_flat: bool,
    overwrite: bool = False,
) -> bool:
    """Write ``email`` into one profile table.

    By default only when that key is absent - the automatic paths (login,
    refresh) fill a blank in, they never relabel a profile behind the operator's
    back. ``overwrite=True`` is for ``profiles set-email``, where replacing the
    value is the explicit ask.

    A targeted line edit rather than a full TOML re-serialize: config.toml is
    hand-maintained here (comments, key order, the signature sub-table), and this
    runs once per profile at onboarding, so rewriting the whole document to add
    one display-only key would be a poor trade.

    Returns True when a line was written, False when the key already exists, the
    section is missing, or the value is empty. Never raises on an unwritable
    file - the caller treats this as best-effort.
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
        lines = config_path.read_text().splitlines(keepends=True)
    except OSError:
        return False
    header = None if legacy_flat else profile
    # Legacy flat config: top-level keys, i.e. everything before the first table.
    start = 0
    if header is not None:
        start = next(
            (i + 1 for i, line in enumerate(lines) if _toml_profile_header(line) == header),
            -1,
        )
        if start < 0:
            return False
    # Walk this section only: stop at the next table header (which for the
    # non-legacy layout also excludes the profile's own [profiles.x.mail.signature]).
    insert_at = start
    for index in range(start, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("["):
            break
        if _toml_key_of(stripped) == "email":
            if not overwrite and _toml_value_of(stripped).strip():
                # Already populated — the automatic paths never relabel a profile.
                # An empty value is a blank to fill, not a label to protect, so the
                # two guards agree with _populate_profile_email_once's `if cfg.email`.
                return False
            lines[index] = f'email = "{_toml_escape(value)}"\n'
            try:
                config_path.write_text("".join(lines))
            except OSError:
                return False
            return True
        insert_at = index + 1
    if insert_at > 0 and lines[insert_at - 1] and not lines[insert_at - 1].endswith("\n"):
        # Inserting after a final line with no trailing newline would concatenate the
        # two into one invalid line, and the write would still report success.
        lines[insert_at - 1] += "\n"
    lines.insert(insert_at, f'email = "{_toml_escape(value)}"\n')
    try:
        config_path.write_text("".join(lines))
    except OSError:
        return False
    return True


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


def _configured_default_name(
    tables: dict[str, dict[str, Any]],
    default_name: str | None,
) -> str | None:
    if len(tables) == 1:
        return next(iter(tables))
    if default_name is not None and default_name in tables:
        return default_name
    return None


def _files_scopes_enabled(file_data: dict[str, Any]) -> bool:
    if "files_scopes" in file_data:
        coerced = _coerce_bool(file_data["files_scopes"])
        if coerced is not None:
            return coerced
    return False


def _google_oauth_client_file(file_data: dict[str, Any]) -> Path | None:
    raw = file_data.get("google_oauth_client_file")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw.strip()).expanduser()


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
    enabled = _coerce_bool(raw.get("enabled"))
    return MailSignatureConfig(
        affiliation=str(raw.get("affiliation") or "").strip(),
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


def _profile_tables(
    file_data: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], str | None, bool]:
    """Return (name → table, default_profile name, legacy_flat)."""
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
        stray = sorted(key for key in file_data if key not in {"default_profile", "profiles"})
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
        return tables, default_name, False

    # Missing / empty config.toml: no profiles (do not invent a phantom "default").
    if not file_data:
        return {}, None, False

    # Legacy: flat top-level keys → one implicit profile named "default".
    legacy_table = {
        key: value for key, value in file_data.items() if key not in {"default_profile", "profiles"}
    }
    if not legacy_table:
        # default_profile alone must not invent a phantom empty "default" profile.
        if "default_profile" in file_data:
            raise ProviderConfigError(
                "default_profile set but no profiles configured; "
                "add [profiles.<name>] entries or flat top-level keys"
            )
        return {}, None, False
    return {_LEGACY_PROFILE_NAME: legacy_table}, _LEGACY_PROFILE_NAME, True


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


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _toml_key_of(stripped_line: str) -> str | None:
    """Bare key name of a ``key = value`` line, or None for comments/tables/blanks."""
    if not stripped_line or stripped_line.startswith(("#", "[")):
        return None
    key, sep, _ = stripped_line.partition("=")
    return key.strip().strip('"').strip("'") if sep else None


def _toml_profile_header(line: str) -> str | None:
    """Profile name from a ``[profiles.<name>]`` header line, else None.

    Tolerates what tomllib accepts and a plain string compare would miss: a
    trailing comment (``[profiles.work]  # main``) and a quoted name
    (``[profiles."work"]``). Missing the header made the backfill a silent no-op
    and made set-email claim the section did not exist.
    """
    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    closing = stripped.find("]")
    if closing == -1:
        return None
    inside = stripped[1:closing].strip()
    prefix = "profiles."
    if not inside.startswith(prefix):
        return None
    name = inside[len(prefix) :].strip()
    if name.startswith(('"', "'")) and name[-1:] == name[:1]:
        # A quoted name is one key, dots included: [profiles."a.b"] is profile "a.b",
        # which _profile_tables allows and is the only way to spell a dotted name.
        return name[1:-1] or None
    # Unquoted dots mean a nested table ([profiles.work.mail.signature]), not a profile.
    return name if name and "." not in name else None


def _toml_value_of(stripped_line: str) -> str:
    """Unquoted value of a ``key = value`` line ("" when blank or unparseable).

    Handles a trailing inline comment, which is valid TOML and plausible in a
    hand-maintained file: ``email = ""  # not yet known`` has to read as blank, or
    the automatic backfill would decline to fill a value tomllib parses as empty.
    """
    _, sep, raw = stripped_line.partition("=")
    if not sep:
        return ""
    raw = raw.strip()
    for quote in ('"', "'"):
        if raw.startswith(quote):
            closing = raw.find(quote, 1)
            return raw[1:closing] if closing != -1 else raw[1:]
    # Bare value: anything from an unquoted ``#`` on is a comment.
    return raw.partition("#")[0].strip()


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


def _wo1162425_scopes_enabled(file_data: dict[str, Any]) -> bool:
    if "wo1162425_scopes" in file_data:
        coerced = _coerce_bool(file_data["wo1162425_scopes"])
        if coerced is not None:
            return coerced
    return False
