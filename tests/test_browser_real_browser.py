"""Real-browser check of launch, attach, fill, upload and detach (issues #465 and #466).

Needs an installed Chrome, Edge or Chromium and `BLUMKIN_BROWSER_TESTS=1`; it is never
run in CI. It launches a headless browser on a throwaway profile, serves a local form
from an ephemeral port, and always stops the browser it started.
"""

from __future__ import annotations

import http.server
import os
import signal
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from blumkin.browser.cdp import attach
from blumkin.browser.discovery import BrowserNotFoundError, resolve_browser
from blumkin.browser.launcher import launch
from blumkin.browser.profiles import create_profile, read_session

pytestmark = pytest.mark.browser

_FORM = b"""<!doctype html><title>Expenses</title>
<label for="amount">Amount</label><input id="amount">
<label for="receipt">Receipt</label><input id="receipt" type="file">
<p>Total due 12.50</p>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_FORM)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def form_url() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_launch_attach_fill_upload_read_and_detach_leaves_the_browser_running(
    tmp_path: Path, form_url: str
) -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    try:
        browser = resolve_browser(None)
    except BrowserNotFoundError:
        pytest.skip("no Chrome, Edge or Chromium installed")
    profile = create_profile(tmp_path, "expense-report")
    receipt = tmp_path / "receipt.png"
    receipt.write_bytes(b"not really a png")
    result = launch(profile, browser, extra_args=("--headless=new", "--no-sandbox"))
    try:
        driver = attach(profile)
        driver.goto(form_url)
        driver.fill("Amount", "12.50")
        driver.upload("Receipt", receipt)
        page_text = driver.read_text()
        assert driver.title() == "Expenses"
        assert "Total due 12.50" in page_text.text
        assert page_text.injection_warning is None
        driver.close()
        assert read_session(profile.path) == result.session
        os.kill(result.session.pid, 0)
        # Detaching must leave the user's session intact: a fresh attach finds the same page.
        again = attach(profile)
        assert again.title() == "Expenses"
        assert again.url == form_url
        again.close()
    finally:
        os.kill(result.session.pid, signal.SIGTERM)
