"""Release Please config must stay pinned to the real package name and version."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_manifest_version_matches_pyproject() -> None:
    manifest = json.loads((_ROOT / ".release-please-manifest.json").read_text(encoding="utf-8"))
    assert manifest["."] == _pyproject()["version"]


def test_config_targets_this_package() -> None:
    config = json.loads((_ROOT / "release-please-config.json").read_text(encoding="utf-8"))
    package = config["packages"]["."]
    name = _pyproject()["name"]
    assert package["package-name"] == name
    assert package["release-type"] == "python"
    assert package["changelog-path"] == "CHANGELOG.md"
    lock_entry = package["extra-files"][0]
    assert lock_entry["path"] == "uv.lock"
    assert name in lock_entry["jsonpath"]


def test_publish_workflow_uses_the_pypi_environment() -> None:
    workflow = (_ROOT / ".github/workflows/release-please.yml").read_text(encoding="utf-8")
    assert "environment: pypi" in workflow
    assert "id-token: write" in workflow
    assert "scripts/verify-pypi-release" in workflow
    assert "scripts/verify-pipx-upgrade" in workflow
