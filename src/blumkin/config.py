"""Load Blumkin config from ~/.config/blumkin (and env overrides)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blumkin.providers.kind import ProviderConfigError, ProviderKind, parse_provider_kind

DEFAULT_GRAPH_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class BlumkinConfig:
    client_id: str
    client_secret: str
    config_dir: Path
    default_tz: str
    files_scopes: bool
    graph_timeout_seconds: float
    mail_signature: MailSignatureConfig
    provider: ProviderKind
    tenant_id: str
    wo1162425_scopes: bool

    @property
    def auth_record_path(self) -> Path:
        return self.config_dir / "auth_record.json"

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def google_token_path(self) -> Path:
        return self.config_dir / "google_token.json"

    @property
    def token_cache_path(self) -> Path:
        return self.config_dir / "msal_token_cache.json"


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
    override = os.environ.get("BLUMKIN_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "blumkin"
    return Path.home() / ".config" / "blumkin"


def load_config() -> BlumkinConfig:
    """Return config; env vars override config.toml keys."""
    directory = config_dir()
    file_data = _read_toml(directory / "config.toml")
    file_values = {key: value for key, value in file_data.items() if isinstance(value, str)}
    client_id = (
        os.environ.get("BLUMKIN_CLIENT_ID", "").strip() or file_values.get("client_id", "").strip()
    )
    client_secret = (
        os.environ.get("BLUMKIN_CLIENT_SECRET", "").strip()
        or file_values.get("client_secret", "").strip()
    )
    tenant_id = (
        os.environ.get("BLUMKIN_TENANT_ID", "").strip() or file_values.get("tenant_id", "").strip()
    )
    default_tz = (
        os.environ.get("BLUMKIN_TZ", "").strip() or file_values.get("default_tz", "").strip()
    )
    return BlumkinConfig(
        client_id=client_id,
        client_secret=client_secret,
        config_dir=directory,
        default_tz=default_tz,
        files_scopes=_files_scopes_enabled(file_data),
        graph_timeout_seconds=_graph_timeout_seconds(file_data),
        mail_signature=_mail_signature_config(file_data),
        provider=_provider_kind(file_data),
        tenant_id=tenant_id,
        wo1162425_scopes=_wo1162425_scopes_enabled(file_data),
    )


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


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


def _files_scopes_enabled(file_data: dict[str, Any]) -> bool:
    env = _env_bool("BLUMKIN_FILES_SCOPES")
    if env is not None:
        return env
    if "files_scopes" in file_data:
        coerced = _coerce_bool(file_data["files_scopes"])
        if coerced is not None:
            return coerced
    return False


def _graph_timeout_seconds(file_data: dict[str, Any]) -> float:
    raw_env = os.environ.get("BLUMKIN_GRAPH_TIMEOUT", "").strip()
    if raw_env:
        try:
            value = float(raw_env)
        except ValueError:
            value = DEFAULT_GRAPH_TIMEOUT_SECONDS
        else:
            if value > 0:
                return value
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


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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


def _wo1162425_scopes_enabled(file_data: dict[str, Any]) -> bool:
    env = _env_bool("BLUMKIN_WO1162425_SCOPES")
    if env is not None:
        return env
    if "wo1162425_scopes" in file_data:
        coerced = _coerce_bool(file_data["wo1162425_scopes"])
        if coerced is not None:
            return coerced
    return False
