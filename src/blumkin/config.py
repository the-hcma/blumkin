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
        for name, table in raw_profiles.items():
            if not isinstance(name, str) or not name.strip():
                raise ProviderConfigError("profile names must be non-empty strings")
            if not isinstance(table, dict):
                raise ProviderConfigError(
                    f"profiles.{name} must be a table in config.toml, got {type(table).__name__}"
                )
            tables[name.strip()] = table
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
