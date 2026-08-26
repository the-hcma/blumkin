"""Load Blumkin config from ~/.config/blumkin (and env overrides)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TENANT_ID = "brk.tech"
DEFAULT_TZ = "America/New_York"


@dataclass(frozen=True, slots=True)
class BlumkinConfig:
    client_id: str
    config_dir: Path
    default_tz: str
    tenant_id: str
    wo1162425_scopes: bool

    @property
    def auth_record_path(self) -> Path:
        return self.config_dir / "auth_record.json"

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def token_cache_path(self) -> Path:
        return self.config_dir / "msal_token_cache.json"


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
    tenant_id = (
        os.environ.get("BLUMKIN_TENANT_ID", "").strip()
        or file_values.get("tenant_id", "").strip()
        or DEFAULT_TENANT_ID
    )
    default_tz = (
        os.environ.get("BLUMKIN_TZ", "").strip()
        or file_values.get("default_tz", "").strip()
        or DEFAULT_TZ
    )
    return BlumkinConfig(
        client_id=client_id,
        config_dir=directory,
        default_tz=default_tz,
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
    return raw in {"1", "true", "yes", "on"}


def _wo1162425_scopes_enabled(file_data: dict[str, Any]) -> bool:
    env = _env_bool("BLUMKIN_WO1162425_SCOPES")
    if env is not None:
        return env
    if "wo1162425_scopes" in file_data:
        coerced = _coerce_bool(file_data["wo1162425_scopes"])
        if coerced is not None:
            return coerced
    return False


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text())
