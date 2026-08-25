"""Load Blumkin config from ~/.config/blumkin (and env overrides)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TENANT_ID = "brk.tech"
DEFAULT_TZ = "America/New_York"


@dataclass(frozen=True, slots=True)
class BlumkinConfig:
    client_id: str
    config_dir: Path
    default_tz: str
    tenant_id: str

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
    file_values = _read_toml(directory / "config.toml")
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
    )


def _read_toml(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text())
    out: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, str):
            out[str(key)] = value
    return out
