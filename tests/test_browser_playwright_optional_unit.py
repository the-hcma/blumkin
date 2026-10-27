"""Playwright is imported only when a browser command runs (issue #517).

A missing or broken Playwright install must not break `blumkin mail` or any other command
that never drives the browser.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from blumkin.browser import cdp
from blumkin.browser.cdp import BrowserAttachError, attach, open_driver
from blumkin.browser.profiles import BrowserSession, create_profile, write_session

#: Runs in a fresh interpreter where every `playwright` import raises ImportError.
_WITHOUT_PLAYWRIGHT = textwrap.dedent(
    """
    import importlib.abc
    import os
    import sys
    import tempfile
    from pathlib import Path

    class _Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "playwright" or name.startswith("playwright."):
                raise ImportError(f"No module named {name!r} (blocked for the test)")
            return None

    sys.meta_path.insert(0, _Blocker())

    cfg_dir = Path(tempfile.mkdtemp())
    (cfg_dir / "config.toml").write_text(
        '[profiles.default]\\nclient_id = "abc"\\n', encoding="utf-8"
    )
    os.environ["BLUMKIN_CONFIG_DIR"] = str(cfg_dir)

    import blumkin.cli
    from click.testing import CliRunner

    assert "playwright" not in sys.modules, "importing blumkin.cli imported playwright"
    for args in (
        ["mail", "--help"],
        ["calendar", "--help"],
        ["browser", "--help"],
        ["mail", "signature", "--json"],
    ):
        result = CliRunner().invoke(blumkin.cli.main, args)
        assert result.exit_code == 0, (args, result.exit_code, result.output)
    assert "playwright" not in sys.modules, "a help or mail path imported playwright"
    print("ok")
    """
)


def test_attach_names_the_missing_playwright_and_how_to_restore_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)  # makes the import fail
    profile = create_profile(tmp_path, "expense-report")
    write_session(
        profile.path,
        BrowserSession(browser="chrome", pid=4242, port=40123, started_at="x"),
    )
    with pytest.raises(BrowserAttachError) as excinfo:
        attach(profile, pid_alive=lambda pid: True)
    message = str(excinfo.value)
    assert "playwright" in message
    assert "uv sync" in message
    assert "\u2014" not in message
    assert "\u2013" not in message


def test_cdp_module_does_not_import_playwright_at_import_time() -> None:
    code = (
        "import sys; import blumkin.browser.cdp; "
        "assert 'playwright' not in sys.modules, 'cdp imported playwright'; print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_cli_import_and_help_paths_work_without_playwright() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _WITHOUT_PLAYWRIGHT],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok")


def test_open_driver_surfaces_the_missing_playwright_as_an_attach_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(BrowserAttachError) as excinfo:
        open_driver("http://127.0.0.1:40123")
    assert "uv sync" in str(excinfo.value)


def test_playwright_error_lookup_falls_back_to_a_type_nothing_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    error_type = cdp._playwright_error()  # pyright: ignore[reportPrivateUsage]
    assert issubclass(error_type, Exception)
    assert error_type is not Exception
